# -*- coding: utf-8 -*-
"""
crypto.py — 词达人接口加解密（整合 easy_cidaren / LangQi99 / Cidaren-AI 三家实现）
- sign 签名: md5(sorted k=v 拼接 + 固定盐)
- jv 解密:  1 (前32位噪声) / 2_xxx (按索引表删位) / 3_xxx (删位+分片重排) / 4 (直接base64)
"""
from __future__ import annotations

import base64
import hashlib
import json

SALT = "ajfajfamsnfaflfasakljdlalkflak"

# ---- jv=2_ 删位表（索引式，来自 easy_cidaren/decryptencrypt/debase64.py）----
JV_TWO = {
    "2_1254":  [0, 1, 2, 4, 5, 36, 47, 48, 59, 96, 107],
    "2_9214":  [0, 1, 2, 4, 5, 6, 7, 48, 49, 66, 149, 150, 284, 374, 375],
    "2_10232": [0, 1, 2, 5, 6, 7, 8, 46, 65, 66, 199, 270, 328, 329],
    "2_10234": [0, 1, 2, 4, 5, 6, 7, 46, 65, 66, 198, 270, 328, 329],
}

# ---- jv=3_ 分片重排表（来自 LangQi99/cidaren/a.py，key 含 n 位删位定义）----
JV_THREE = {
    "3_1021": {
        "uc": [{"s": 0, "n": 1}, {"s": 1, "n": 2}, {"s": 33, "n": 1}, {"s": 57, "n": 1}, {"s": 111, "n": 1}],
        "avg": 5, "loc": [1, 3, 2, 0, 4],
    },
    "3_2265": {
        "uc": [{"s": 0, "n": 2}, {"s": 1, "n": 3}, {"s": 33, "n": 1}, {"s": 57, "n": 1}, {"s": 121, "n": 1}],
        "avg": 5, "loc": [3, 1, 0, 4, 2],
    },
    "3_2277": {
        "uc": [{"s": 0, "n": 3}, {"s": 1, "n": 3}, {"s": 32, "n": 2}, {"s": 50, "n": 1}, {"s": 110, "n": 1}],
        "avg": 5, "loc": [3, 1, 0, 4, 2],
    },
}

# ---- jv=2_ 删位表（区间式，来自 LangQi99/cidaren/a.py，补充覆盖）----
JV_TWO_RANGES = {
    "2_1254":  [{"s": 0, "n": 3}, {"s": 1, "n": 2}, {"s": 31, "n": 1}, {"s": 41, "n": 2}, {"s": 51, "n": 1}, {"s": 87, "n": 1}, {"s": 97, "n": 1}],
    "2_10234": [{"s": 0, "n": 3}, {"s": 1, "n": 4}, {"s": 39, "n": 1}, {"s": 57, "n": 2}, {"s": 188, "n": 1}, {"s": 259, "n": 1}, {"s": 316, "n": 2}],
    "2_9214":  [{"s": 0, "n": 3}, {"s": 1, "n": 4}, {"s": 41, "n": 2}, {"s": 57, "n": 1}, {"s": 139, "n": 2}, {"s": 272, "n": 1}, {"s": 361, "n": 2}],
    "2_9314":  [{"s": 0, "n": 3}, {"s": 1, "n": 4}, {"s": 31, "n": 2}, {"s": 60, "n": 1}, {"s": 152, "n": 2}, {"s": 256, "n": 1}],
}

DEFAULT_JV_TWO = JV_TWO["2_9214"]


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def sign(params: dict) -> str:
    """POST 请求签名：sorted(k=v) 拼接 + 盐，再 md5。dict/list 值做紧凑 JSON。"""
    parts = []
    for k in sorted(params):
        v = params[k]
        if isinstance(v, (dict, list)):
            v = json.dumps(v, separators=(",", ":"), ensure_ascii=False)
        if v or v == 0:
            parts.append(f"{k}={v}")
    return md5("&".join(parts) + SALT)


def _b64d(s: str) -> str:
    s = s.strip().replace(" ", "")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", errors="ignore")


def _pluck_ranges(d: str, rules) -> str:
    """按区间式规则 [(s,n), ...] 依次删位。"""
    for r in rules:
        s, n = r["s"], r["n"]
        d = (d[:s] if s else "") + d[s + n:]
    return d


def _pluck_indexes(d: str, indexes) -> str:
    """按索引式规则 [i, ...] 一次性删位。"""
    chars = list(d)
    for i in sorted(indexes, reverse=True):
        if 0 <= i < len(chars):
            del chars[i]
    return "".join(chars)


def strip_noise(data: str, jv: str) -> str:
    """去除 jv 对应的噪声字符，返回可 base64 的字符串。"""
    jv = str(jv)
    if jv.startswith("3_") and jv in JV_THREE:
        cfg = JV_THREE[jv]
        d = _pluck_ranges(data, cfg["uc"])
        avg, loc = cfg["avg"], cfg["loc"]
        chunk = len(d) // avg
        pieces = [d[i * chunk:(i + 1) * chunk] for i in range(avg)]
        out = "".join(pieces[loc.index(i)] for i in range(avg))
        if len(d) % chunk:
            out += d[avg * chunk:]
        return out
    if jv in JV_TWO:
        return _pluck_indexes(data, JV_TWO[jv])
    if jv in JV_TWO_RANGES:
        return _pluck_ranges(data, JV_TWO_RANGES[jv])
    # 未知 jv：按默认表尝试
    return _pluck_indexes(data, DEFAULT_JV_TWO)


def decrypt_data(data, jv):
    """解密响应体 data 字段。data 可能是 str（需解噪+base64）或已是 dict。"""
    if data is None:
        return None
    if isinstance(data, dict):
        return data
    if not isinstance(data, str) or not data:
        return data
    jv = str(jv or "")
    if not jv or jv == "0" or jv == "99":
        # 未加密，尝试直接 json
        try:
            return json.loads(data)
        except (ValueError, TypeError):
            return data
    if jv == "1":
        raw = data[32:]
    elif jv == "4":
        raw = data
    elif jv.startswith("2_") or jv.startswith("3_"):
        raw = strip_noise(data, jv)
    else:
        raw = data
    try:
        return json.loads(_b64d(raw))
    except (ValueError, TypeError):
        # 再试一次：去掉前导非 JSON 噪音
        try:
            import re
            m = re.search(r"\{.*", _b64d(raw), re.S)
            if m:
                return json.loads(m.group(0))
        except (ValueError, TypeError):
            pass
        return data


def decrypt_response(resp: dict) -> dict:
    """原地解密整个响应，兼容各家实现。"""
    if not isinstance(resp, dict):
        return resp
    if "data" in resp and "jv" in resp:
        resp = dict(resp)
        resp["data"] = decrypt_data(resp.get("data"), resp.get("jv"))
    return resp
