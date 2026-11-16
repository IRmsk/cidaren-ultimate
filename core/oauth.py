# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-only
"""Cidaren OAuth V2 / ECDH login protocol.

The WeChat authorization code is exchanged for an account token and a
process-local ``jv=99`` crypto document. The implementation follows the
current H5 protocol used by MOPELotus/Easy_Cidaren_Backend.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import parse_qsl, quote, urlsplit

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

WECHAT_APP_ID = "wx2a694105a6abbe6d"
WECHAT_SCOPE = "snsapi_userinfo"
LOGIN_VERSION = "2.7.0.261019_01"
CRYPTO_VERSION = "v1"
SIGNING_SUFFIX = "ajfajfamsnfaflfasakljdlalkflak"
LOGIN_URL = "https://app.vocabgo.com/student/api/Auth/Wechat/V2/LoginByWechatCode"
STUDENT_MAIN_URL = "https://app.vocabgo.com/student/api/Student/Main"
STUDENT_URL = "https://app.vocabgo.com/student/"
LOGIN_AAD = b"vcg-auth:POST:/Wechat/V2/LoginByWechatCode"
LOGIN_HKDF_INFO = b"vcg-auth-aes"
OAUTH_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
_VALUE = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")


class OAuthError(RuntimeError):
    """Safe failure from the Cidaren OAuth flow."""


@dataclass(frozen=True)
class OAuthAuthorization:
    authorization_url: str
    state_digest: str
    marker_digest: str


@dataclass(frozen=True)
class OAuthSession:
    token: str
    crypto_document: Mapping[str, object]
    user_info: Mapping[str, object] = field(default_factory=dict)


def _digest(domain: bytes, value: str) -> str:
    return hashlib.sha256(domain + value.encode("ascii")).hexdigest()


def begin_authorization() -> OAuthAuthorization:
    """Create a state-bound WeChat authorization URL."""
    state = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    marker = "x" + secrets.token_hex(16)
    redirect = quote(f"{STUDENT_URL}?authorize={marker}", safe="")
    url = (
        "https://open.weixin.qq.com/connect/oauth2/authorize"
        f"?appid={WECHAT_APP_ID}&redirect_uri={redirect}"
        f"&response_type=code&scope={WECHAT_SCOPE}&state={state}#wechat_redirect"
    )
    return OAuthAuthorization(
        authorization_url=url,
        state_digest=_digest(b"asterism:cidaren:oauth-state:v1\0", state),
        marker_digest=_digest(b"asterism:cidaren:authorize-marker:v1\0", marker),
    )


def _callback_values(callback_url: str) -> dict[str, str]:
    if not callback_url or len(callback_url) > 16 * 1024 or callback_url.strip() != callback_url:
        raise OAuthError("invalid Cidaren OAuth callback URL")
    try:
        parsed = urlsplit(callback_url)
        port = parsed.port
    except ValueError as exc:
        raise OAuthError("invalid Cidaren OAuth callback URL") from exc
    if parsed.scheme != "https" or parsed.hostname != "app.vocabgo.com" or port is not None:
        raise OAuthError("invalid Cidaren OAuth callback origin")
    if parsed.username is not None or parsed.password is not None or parsed.path not in ("/student", "/student/"):
        raise OAuthError("invalid Cidaren OAuth callback path")

    pairs = list(parse_qsl(parsed.query, keep_blank_values=True))
    if "?" in parsed.fragment:
        pairs.extend(parse_qsl(parsed.fragment.split("?", 1)[1], keep_blank_values=True))
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in ("code", "state", "authorize"):
            continue
        if key in values or not _VALUE.fullmatch(value):
            raise OAuthError("ambiguous Cidaren OAuth callback")
        values[key] = value
    if set(values) != {"code", "state", "authorize"} or values["authorize"] == "2":
        raise OAuthError("incomplete Cidaren OAuth callback")
    return values


def parse_callback(callback_url: str, state_digest: str, marker_digest: str) -> str:
    """Validate a copied callback URL and return its one-time code."""
    values = _callback_values(callback_url)
    actual_state = _digest(b"asterism:cidaren:oauth-state:v1\0", values["state"])
    actual_marker = _digest(b"asterism:cidaren:authorize-marker:v1\0", values["authorize"])
    if not hmac.compare_digest(actual_state, state_digest) or not hmac.compare_digest(
        actual_marker, marker_digest
    ):
        raise OAuthError("Cidaren OAuth callback does not match the pending login")
    return values["code"]


def _headers(token: str = "") -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Abc": hashlib.md5(OAUTH_USER_AGENT.encode("utf-8")).hexdigest(),
        "Authorization-v": "00",
        "UserToken": token,
        "X-Requested-With": "XMLHttpRequest",
        "User-Agent": OAUTH_USER_AGENT,
        "Accept-Language": "*",
        "Referer": STUDENT_URL,
        "Origin": "https://app.vocabgo.com",
        "Content-Type": "application/json;charset=UTF-8",
    }


def exchange_callback(
    callback_url: str,
    state_digest: str,
    marker_digest: str,
    *,
    session: requests.Session | None = None,
    timeout: float = 20.0,
) -> OAuthSession:
    """Exchange a validated WeChat callback for token and ECDH crypto context."""
    code = parse_callback(callback_url, state_digest, marker_digest)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_key = base64.b64encode(public_der).decode("ascii")
    timestamp = int(time.time() * 1000)
    signature = (
        f"code={code}&cpub_k={public_key}&cpub_v={CRYPTO_VERSION}"
        f"&timestamp={timestamp}&version={LOGIN_VERSION}{SIGNING_SUFFIX}"
    )
    body = {
        "code": code,
        "cpub_k": public_key,
        "cpub_v": CRYPTO_VERSION,
        "timestamp": timestamp,
        "version": LOGIN_VERSION,
        "sign": hashlib.md5(signature.encode("utf-8")).hexdigest(),
        "app_type": 1,
    }
    client = session or requests.Session()
    response = client.post(LOGIN_URL, headers=_headers(), json=body, timeout=timeout)
    response.raise_for_status()
    result = response.json()
    if result.get("code") != 1:
        raise OAuthError("Cidaren rejected or expired the WeChat OAuth code")
    data = result.get("data")
    if not isinstance(data, dict) or data.get("handshake_required") is not False or data.get("encrypted") is not True:
        raise OAuthError("Cidaren OAuth response shape changed")
    handshake, payload = data.get("handshake"), data.get("payload")
    if not isinstance(handshake, dict) or not isinstance(payload, dict):
        raise OAuthError("Cidaren OAuth response is incomplete")
    if handshake.get("version") != CRYPTO_VERSION or payload.get("version") != CRYPTO_VERSION:
        raise OAuthError("Cidaren OAuth crypto version changed")

    try:
        server_der = base64.b64decode(handshake["spub_k"], validate=True)
        server_public = serialization.load_der_public_key(server_der)
        salt = base64.b64decode(handshake["salt"], validate=True)
        iv = base64.b64decode(payload["iv"], validate=True)
        ciphertext = base64.b64decode(payload.get("cipher_text") or payload.get("cipherText"), validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise OAuthError("Cidaren OAuth response contains malformed crypto fields") from exc
    if not isinstance(server_public, ec.EllipticCurvePublicKey) or not isinstance(
        server_public.curve, ec.SECP256R1
    ):
        raise OAuthError("Cidaren OAuth server key is invalid")
    if payload.get("aad") != LOGIN_AAD.decode("ascii"):
        raise OAuthError("Cidaren OAuth authenticated context changed")

    shared_secret = private_key.exchange(ec.ECDH(), server_public)
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=LOGIN_HKDF_INFO).derive(shared_secret)
    plaintext = AESGCM(key).decrypt(iv, ciphertext, LOGIN_AAD)
    login = json.loads(plaintext)
    token = login.get("token") if isinstance(login, dict) else None
    if not isinstance(token, str) or not token.strip():
        raise OAuthError("Cidaren OAuth response contains no token")

    verify = client.get(
        STUDENT_MAIN_URL,
        headers=_headers(token),
        params={"timestamp": int(time.time() * 1000), "version": LOGIN_VERSION, "app_type": 1},
        timeout=timeout,
    )
    verify.raise_for_status()
    verify_result = verify.json()
    if verify_result.get("code") != 1:
        raise OAuthError("Cidaren OAuth session validation failed")
    user_info = (verify_result.get("data") or {}).get("user_info") or {}
    if not isinstance(user_info, Mapping):
        user_info = {}
    return OAuthSession(
        token=token,
        crypto_document={
            "login_info": {
                "a": "h" + base64.b64encode(shared_secret).decode("ascii"),
                "b": "a" + base64.b64encode(salt).decode("ascii"),
            }
        },
        user_info=dict(user_info),
    )
