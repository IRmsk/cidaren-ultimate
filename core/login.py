# -*- coding: utf-8 -*-
"""Cidaren OAuth V2 login helpers.

The old QR flow only waited for a manually supplied ``USERTOKEN``. The V2
flow binds a WeChat authorization code to an ECDH shared secret, so a
successful login produces both ``USERTOKEN`` and ``config/crypto.json``.
"""
from __future__ import annotations

import base64
import io
import time

from core.api import token_valid
from core.config import get_runtime_config, save_runtime_config
from core.crypto import md5, write_crypto_document
from core.oauth import (
    LOGIN_VERSION,
    OAUTH_USER_AGENT,
    OAuthSession,
    begin_authorization,
    exchange_callback,
)

VERSION = LOGIN_VERSION
BUSINESS_AUTH_V = md5("0")


def _qr_png(value: str) -> str | None:
    try:
        import qrcode

        qr = qrcode.QRCode(
            version=3,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=7,
            border=2,
        )
        qr.add_data(value)
        qr.make(fit=True)
        image = qr.make_image()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except ImportError:
        return None


def login_start() -> dict:
    """Create a state-bound OAuth V2 authorization URL and QR image."""
    authorization = begin_authorization()
    return {
        "authorization_url": authorization.authorization_url,
        "redirect_url": authorization.authorization_url,
        "state_digest": authorization.state_digest,
        "marker_digest": authorization.marker_digest,
        "qr_base64": _qr_png(authorization.authorization_url),
        "expires_at": time.time() + 600,
    }


def complete_login(
    callback_url: str,
    state_digest: str,
    marker_digest: str,
    *,
    session=None,
    crypto_file=None,
    timeout: float = 20.0,
) -> dict:
    """Exchange the callback, persist credentials, and return token metadata."""
    oauth_session: OAuthSession = exchange_callback(
        callback_url,
        state_digest,
        marker_digest,
        session=session,
        timeout=timeout,
    )
    crypto_path = write_crypto_document(
        oauth_session.crypto_document,
        path=crypto_file,
        token=oauth_session.token,
    )

    config = get_runtime_config()
    config.update(
        {
            "USERTOKEN": oauth_session.token,
            "ABC": md5(OAUTH_USER_AGENT),
            "AUTH_V": BUSINESS_AUTH_V,
            "TOKEN_SAVED_AT": str(time.time()),
        }
    )
    save_runtime_config(config)

    user_info = dict(oauth_session.user_info or {})
    name = user_info.get("student_name") or user_info.get("nick_name") or "?"
    return {
        "token": oauth_session.token,
        "name": name,
        "user_info": user_info,
        "crypto_file": str(crypto_path),
        "version": VERSION,
    }


def is_token_valid(user_token: str, abc: str = "", auth_v: str = "") -> tuple:
    """Validate a token through the current student API."""
    return token_valid(user_token, abc, auth_v)


def _gateway_token_valid(user_token: str) -> bool:
    """Compatibility stub for callers from the retired token-polling flow."""
    return False
