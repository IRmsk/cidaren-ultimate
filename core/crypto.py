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
import os
from pathlib import Path

SALT = "ajfajfamsnfaflfasakljdlalkflak"

# ---- jv=99 加密上下文（进程内注入）----
_crypto_context = None


def set_crypto_document(document):
    """进程内注入加密上下文（登录成功后调用）。"""
    global _crypto_context
    if isinstance(document, bytes):
        document = document.decode("utf-8")
    _crypto_context = json.loads(document) if isinstance(document, str) else document


def _decode_prefixed_base64(value, prefix):
    if not value:
        return None
    if value.startswith(prefix):
        value = value[1:]
    return base64.b64decode(value)


def _read_crypto_context():
    """读取加密上下文：优先进程内注入，其次从 crypto.json 文件读。"""
    global _crypto_context
    if _crypto_context is not None:
        login_info = _crypto_context.get("login_info") or _crypto_context
        shared_secret = _decode_prefixed_base64(login_info.get("a", ""), "h")
        salt = _decode_prefixed_base64(login_info.get("b", ""), "a")
        if shared_secret and salt:
            return shared_secret, salt

    # 从文件读
    env_path = os.environ.get("CIDAREN_CRYPTO_FILE")
    candidates = []
    if env_path:
        candidates.append(Path(env_path))
    root = Path(__file__).resolve().parent.parent
    candidates.extend([
        root / "config" / "crypto.json",
        root / "crypto.json",
    ])
    for path in candidates:
        if not path.exists():
            continue
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        login_info = data.get("login_info") or data
        shared_secret = _decode_prefixed_base64(login_info.get("a", ""), "h")
        salt = _decode_prefixed_base64(login_info.get("b", ""), "a")
        if shared_secret and salt:
            return shared_secret, salt

    raise RuntimeError(
        "收到 jv=99 加密响应，但未找到加密密钥。"
        "请用新版登录获取 crypto.json，或通过 set_crypto_document 注入。"
    )


def _decrypt_jv99(payload):
    """jv=99 AES-GCM 解密（参考 MOPELotus/Easy_Cidaren_Backend）。"""
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        raise RuntimeError("jv=99 解密需要 cryptography 库，请先 pip install cryptography。")

    if isinstance(payload, dict) and payload.get("payload"):
        payload = payload["payload"]
    if not isinstance(payload, dict):
        raise ValueError("jv=99 响应不是有效的加密 payload。")

    iv = payload.get("iv")
    cipher_text = payload.get("cipher_text") or payload.get("cipherText")
    aad = payload.get("aad") or ""
    if not iv or not cipher_text or not aad:
        raise ValueError("jv=99 响应缺少 iv/cipher_text/aad。")

    ucd = aad.split(":")[-1]
    if not ucd:
        raise ValueError("jv=99 响应 aad 缺少会话标识。")

    shared_secret, salt = _read_crypto_context()
    info = f"vcg-exam-aes:{ucd}".encode("utf-8")
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=info,
    ).derive(shared_secret)

    plain = AESGCM(key).decrypt(
        base64.b64decode(iv),
        base64.b64decode(cipher_text),
        aad.encode("utf-8"),
    )
    text = plain.decode("utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text

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
    if not jv or jv == "0":
        # 未加密，尝试直接 json
        try:
            return json.loads(data)
        except (ValueError, TypeError):
            return data
    if jv == "99":
        # 新版 AES-GCM 加密
        return _decrypt_jv99(data)
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
