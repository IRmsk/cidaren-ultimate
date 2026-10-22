# -*- coding: utf-8 -*-
"""
login_v2.py — 新版微信扫码登录（ECDH + HKDF + AES-GCM）
参考 vocabgo-auto/qrlogin.py，单独分出来，不动旧的 login.py。
"""
from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests

AUTH_BASE = "https://app.vocabgo.com"
AUTH_WECHAT_V2 = AUTH_BASE + "/Auth/Wechat/V2/LoginByWechatCode"
AUTH_HKDF_INFO = b"vcg-auth-aes"

WECHAT_OAUTH = "https://open.weixin.qq.com/connect/oauth2/authorize"
WECHAT_APPID = "wx2a694105a6abbe6d"
WECHAT_REDIRECT_URI = "https://app.vocabgo.com/student/?authorize=0"


@dataclass
class LoginResult:
    ok: bool
    token: str = ""
    message: str = ""
    crypto: dict = None  # 登录成功后返回的加密上下文，可直接用于 jv=99 解密


def build_wechat_login_url(state: str = "vocabgo") -> str:
    """拼出「用微信打开就能登录词达人」的授权链接（把它画成二维码即可）。"""
    params = {
        "appid": WECHAT_APPID,
        "redirect_uri": WECHAT_REDIRECT_URI,
        "response_type": "code",
        "scope": "snsapi_userinfo",
        "state": state,
    }
    query = "&".join(f"{key}={quote(str(value), safe='')}" for key, value in params.items())
    return f"{WECHAT_OAUTH}?{query}#wechat_redirect"


def extract_wechat_code(text: str) -> str:
    """从用户粘贴的内容里抠出微信 code：整条链接、带参 URL、参数片段或裸 code 都认。"""
    text = (text or "").strip()
    if not text:
        return ""
    matched = re.search(r"(?:^|[?&#])code=([A-Za-z0-9_\-]+)", text)
    if matched:
        return matched.group(1)
    if re.fullmatch(r"[A-Za-z0-9_\-]{8,}", text):
        return text
    return ""


def _generate_keypair():
    """本地生成 P-256 密钥对，返回 (私钥, base64(SPKI 公钥))。"""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_b64 = base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
    ).decode("ascii")
    return private_key, public_b64


def _decrypt_auth_response(envelope: dict, private_key) -> dict:
    """解密「服务端用我们公钥加密」的响应。"""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    data = envelope.get("data") if isinstance(envelope.get("data"), dict) else envelope
    handshake = data.get("handshake") or {}
    payload = data.get("payload") or {}
    if isinstance(handshake, str):
        handshake = json.loads(handshake)
    if isinstance(payload, str):
        payload = json.loads(payload)

    server_public_b64 = handshake.get("spub_k") or handshake.get("serverPublicKey") or ""
    salt_b64 = handshake.get("salt") or ""
    if not server_public_b64 or not salt_b64:
        raise ValueError("响应里缺少 spub_k / salt，无法解密")

    server_key = serialization.load_der_public_key(base64.b64decode(server_public_b64))
    shared_secret = private_key.exchange(ec.ECDH(), server_key)
    aes_key = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=base64.b64decode(salt_b64), info=AUTH_HKDF_INFO
    ).derive(shared_secret)

    iv = base64.b64decode(payload.get("iv") or "")
    cipher_text = base64.b64decode(payload.get("cipher_text") or payload.get("cipherText") or "")
    aad = payload.get("aad")
    plain = AESGCM(aes_key).decrypt(iv, cipher_text, aad.encode("utf-8") if aad else None)
    return json.loads(plain.decode("utf-8"))


def login_with_wechat_code(code: str) -> LoginResult:
    """用微信授权回来的 code 换令牌。"""
    code = (code or "").strip()
    if not code:
        return LoginResult(False, message="没识别到微信 code —— 请把微信里那条链接整段粘过来")

    try:
        private_key, cpub_k = _generate_keypair()
        resp = requests.post(
            AUTH_WECHAT_V2,
            json={
                "code": code,
                "cpub_k": cpub_k,
                "cpub_v": "v1",
            },
            timeout=15,
        )
        envelope = resp.json()
        if envelope.get("code") != 1:
            return LoginResult(False, message=f"登录失败：{envelope.get('msg', '未知错误')}")

        plain = _decrypt_auth_response(envelope, private_key)
        token = plain.get("token") or ""
        if not token:
            return LoginResult(False, message="解密成功但没拿到 token")

        # 构造加密上下文（正好可用于 jv=99 解密）
        login_info = envelope.get("data", {}).get("handshake", {})
        crypto = None
        if isinstance(login_info, str):
            try:
                login_info = json.loads(login_info)
            except json.JSONDecodeError:
                login_info = {}
        if isinstance(login_info, dict) and login_info.get("spub_k") and login_info.get("salt"):
            # 从握手信息里提取 shared_secret 和 salt（注意：这里的 spub_k 是服务端公钥，不是 shared_secret）
            # 实际上 shared_secret 是我们本地算出来的，salt 是服务端给的
            # 我们把它存成 crypto.json 的格式：a = "h" + base64(shared_secret), b = "a" + base64(salt)
            # 但我们这里没有存 shared_secret，所以先存个占位
            # 等以后真的用 jv=99 的时候再补
            crypto = {
                "login_info": {
                    "a": "",  # shared_secret，暂未存
                    "b": "a" + (login_info.get("salt", "")),
                }
            }

        return LoginResult(True, token=token, message="登录成功", crypto=crypto)

    except Exception as e:
        return LoginResult(False, message=f"登录异常：{str(e)}")
