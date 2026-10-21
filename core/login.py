# -*- coding: utf-8 -*-
"""
login.py — 微信扫码登录（移植 Rain-shadow/cdr 的授权流程，可选，免 Fiddler 抓包）

原理：本地生成 user_token -> 请求授权接口拿 redirect_url -> 生成二维码
      微信扫码授权后 token 立即生效。仅能获取 USERTOKEN；ABC/AUTH_V 仍可在配置面板补充。
"""
from __future__ import annotations

import base64
import hashlib
import random
import time

import requests

from .api import Client, token_valid
from .crypto import md5

GATEWAY = "https://gateway.vocabgo.com"
VERSION = "1.2.0"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
)
SALT = "ajfajfamsnfaflfasakljdlalkflak"


def _ms() -> int:
    return int(time.time() * 1000)


def _random_user_token() -> str:
    pass_list = "abcde0123456789"
    token = "".join(random.choice(pass_list) for _ in range(32))
    return md5(f"{token}{_ms()}")


def _authorize(user_token: str) -> str:
    """请求授权接口，返回 redirect_url（微信登录链接）。"""
    now_time = _ms()
    sign = md5(
        f"auth_type=1&return_url=https://app.vocabgo.com/overall/#/student/home"
        f"&timestamp={now_time}&user_token={user_token}&versions={VERSION}{SALT}"
    )
    data = {
        "return_url": "https://app.vocabgo.com/overall/#/student/home",
        "auth_type": 1,
        "user_token": user_token,
        "timestamp": now_time,
        "versions": VERSION,
        "sign": sign,
    }
    headers = {
        "Content-Type": "application/json;charset=UTF-8",
        "Host": "app.vocabgo.com",
        "Origin": "https://app.vocabgo.com",
        "Referer": "https://app.vocabgo.com/",
        "User-Agent": UA,
        "X-DevTools-Emulate-Network-Conditions-Client-Id": md5(UA).upper(),
        "X-Requested-With": "XMLHttpRequest",
    }
    resp = requests.post(f"{GATEWAY}/Auth/Thirdpart/Authorize", headers=headers, json=data, timeout=20)
    j = resp.json()
    data_obj = j.get("data") or {}
    redirect_url = data_obj.get("redirect_url") or ""
    if not redirect_url:
        raise RuntimeError(f"授权接口异常: {j}")
    return redirect_url


def _gateway_token_valid(user_token: str) -> bool:
    """用旧网关接口验证 token（扫码登录配套）。"""
    headers = {
        "UserToken": user_token,
        "User-Agent": UA,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }
    try:
        resp = requests.get(
            f"{GATEWAY}/Student/Main?timestamp={_ms()}&versions={VERSION}",
            headers=headers, timeout=20,
        )
        return resp.json().get("code") == 1
    except Exception:
        return False


def is_token_valid(user_token: str, abc: str = "", auth_v: str = "") -> tuple:
    """先新接口后旧接口验证。返回 (ok, info)。"""
    ok, info = token_valid(user_token, abc, auth_v)
    if ok:
        return True, info
    if _gateway_token_valid(user_token):
        return True, {"student_name": "?"}
    return False, None


def qr_authorize() -> dict:
    """
    生成扫码授权会话。返回:
      {user_token, redirect_url, qr_base64(可选), expire_at}
    调用方应轮询 is_token_valid(user_token) 直到生效。
    """
    user_token = _random_user_token()
    redirect_url = _authorize(user_token)
    result = {
        "user_token": user_token,
        "redirect_url": redirect_url,
        "expire_at": time.time() + 600,
    }
    try:
        import qrcode  # 可选依赖
        qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_L,
                           box_size=6, border=2)
        qr.add_data(redirect_url)
        qr.make(fit=True)
        img = qr.make_image()
        import io
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        result["qr_base64"] = base64.b64encode(buf.getvalue()).decode()
    except ImportError:
        result["qr_base64"] = None
    return result
