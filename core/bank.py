# -*- coding: utf-8 -*-
"""
bank.py — 本地答案库（题库指纹 + 短语/单词答案 + 离线释义词典）
- bank:       题目指纹 -> {"ans": 答案, "stem": 题干}    （历史积累的题目指纹库）
- phrase_lib: 中文释义 -> [完整短语, ...]                （短语答案库）
- word_lib:   中文释义 -> [单词/变体, ...]               （单词答案库）
- dict_zh:    英文单词 -> [中文释义, ...]                （离线释义词典，ECDICT 开源数据）
判错后自动从服务端 answer_corrects 学习，越用越准。文件锁 + pid 临时文件保证多进程安全。
"""
from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent / "data"
BANK_FILE = DATA_DIR / "bank.json"
ANSWER_LIB_FILE = DATA_DIR / "answer_lib.json"
WORD_POOL_FILE = DATA_DIR / "word_pool.json"
# 历史积累的题目指纹库（key = mode::stem::remark，3 段）
BANK_LEGACY_FILE = DATA_DIR / "bank_legacy.json"
# 离线释义词典：英文单词 -> 中文释义（来源：ECDICT 开源英汉词典，MIT 协议）
DICT_ZH_FILE = DATA_DIR / "dict_zh.json"
WORD_KEY = "_word_answers"

_lock = threading.Lock()
_dict_zh_cache: dict | None = None


def _ensure_dir():
    DATA_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def _file_lock(lock_path):
    with open(lock_path, "a+b") as lf:
        if os.name == "nt":
            import msvcrt
            lf.seek(0)
            msvcrt.locking(lf.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lf.seek(0)
                msvcrt.locking(lf.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)


def _atomic_write(path: Path, obj: dict):
    """文件锁 + pid 临时文件 + os.replace，多进程安全。"""
    _ensure_dir()
    lock_path = str(path) + ".lock"
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with _file_lock(lock_path):
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(obj, f, ensure_ascii=False, indent=1)
            os.replace(tmp_path, path)
        finally:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (ValueError, OSError):
        return {}


# ---- 题库（题目指纹缓存）----

def bank_load() -> dict:
    """合并新格式 bank.json + 旧格式 bank_legacy.json（3段 key）。"""
    bank = _read_json(BANK_FILE)
    legacy = _read_json(BANK_LEGACY_FILE)
    for k, v in legacy.items():
        if k not in bank and isinstance(v, dict) and "ans" in v:
            bank[k] = v
    return bank


def bank_save(bank: dict):
    _atomic_write(BANK_FILE, bank)


def bank_add(key: str, ans, stem: str = ""):
    if not key or ans is None:
        return
    with _lock:
        bank = bank_load()
        bank[key] = {"ans": ans, "stem": stem}
        bank_save(bank)


def legacy_key_of(key: str) -> str | None:
    """把新格式 key (mode::kind::sn::remark::opts) 转成旧格式 (mode::sn::remark) 用于查历史题库。"""
    parts = key.split("::")
    if len(parts) == 5:
        return f"{parts[0]}::{parts[2]}::{parts[3]}"
    return None


def _remark_of(key: str) -> str | None:
    """从指纹 key 提取中文备注段（32/73 型题的稳定标识；题干占位/选项每次可能变化）。"""
    parts = key.split("::")
    if len(parts) == 5:
        return parts[3]
    if len(parts) >= 3:
        return parts[2]
    return None


def bank_lookup(key: str, bank: dict):
    """先查新格式 key，未命中再尝试旧格式，最后按 mode+remark 语义遍历兜底。

    语义兜底解决：32/73 型题的题干下划线占位、选项顺序可能因服务端重刷而变化，
    导致指纹失配；而中文短语（remark）每次固定，遍历同题型下同 remark 的已存答案。
    """
    if key in bank:
        return bank[key]["ans"]
    legacy = legacy_key_of(key)
    if legacy and legacy in bank:
        return bank[legacy]["ans"]
    parts = key.split("::")
    mode = parts[0] if parts else ""
    rem = _remark_of(key)
    if mode and rem and len(rem) > 1:
        needle = "::" + rem
        for k, v in bank.items():
            if not k.startswith(mode + "::"):
                continue
            if (needle + "::") in k or k.endswith(needle):
                return v["ans"]
    return None


def load_word_pool() -> dict:
    """加载全局词表（word_zh -> URL编码英文短语）。"""
    return _read_json(WORD_POOL_FILE)


def load_dict_zh() -> dict:
    """加载离线释义词典（英文单词 -> 中文释义列表），带模块级缓存。"""
    global _dict_zh_cache
    if _dict_zh_cache is None:
        try:
            _dict_zh_cache = _read_json(DICT_ZH_FILE) or {}
        except Exception:
            _dict_zh_cache = {}
    return _dict_zh_cache


# ---- 短语/单词答案库（easy_cidaren 格式）----

def _lib() -> dict:
    return _read_json(ANSWER_LIB_FILE)


def _normalize_zh(text: str) -> str:
    """标准化中文短语：去掉标点、空格，用于模糊匹配。"""
    import re
    if not text:
        return ""
    # 去掉所有标点和空格
    return re.sub(r'[^\w\u4e00-\u9fff]', '', text).lower()


def lookup_phrase(word_zh) -> list | None:
    """短语库命中（mode 32 / 73 完整答案）。先精确匹配，再模糊匹配。"""
    if not word_zh:
        return None
    # 1. 精确匹配
    answers = _lib().get(word_zh)
    if answers:
        return answers if isinstance(answers, list) else [answers]
    # 2. 模糊匹配（去掉标点、空格后再比）
    norm_key = _normalize_zh(word_zh)
    if not norm_key:
        return None
    for k, v in _lib().items():
        if k == WORD_KEY:
            continue
        if _normalize_zh(k) == norm_key:
            return v if isinstance(v, list) else [v]
    # 3. 部分匹配（包含关系）
    for k, v in _lib().items():
        if k == WORD_KEY:
            continue
        nk = _normalize_zh(k)
        if norm_key in nk or nk in norm_key:
            return v if isinstance(v, list) else [v]
    return None


def lookup_word(word_zh) -> list | None:
    """单词库命中（mode 42/73 单词/同义变体）。"""
    if not word_zh:
        return None
    answers = _lib().get(WORD_KEY, {}).get(word_zh)
    if not answers:
        return None
    return answers if isinstance(answers, list) else [answers]


def add_phrase(word_zh, corrects):
    """判错后学习完整短语（mode 32 answer_corrects）。"""
    if not word_zh or not corrects:
        return
    answers = [c for c in corrects if isinstance(c, str) and c]
    if not answers:
        return
    with _lock:
        lib = _lib()
        old = lib.get(word_zh, [])
        if not isinstance(old, list):
            old = [old]
        added = False
        for a in answers:
            if a not in old:
                old.append(a)
                added = True
        if not added:
            return
        lib[word_zh] = old
        _atomic_write(ANSWER_LIB_FILE, lib)


def add_words(word_zh, words):
    """判错后学习单词（mode 42/73 的 answer_corrects 选项单词）。"""
    if not word_zh or not words:
        return
    word_answers = [w for w in words if isinstance(w, str) and w]
    if not word_answers:
        return
    with _lock:
        lib = _lib()
        word_map = lib.get(WORD_KEY, {})
        if not isinstance(word_map, dict):
            word_map = {}
        old = word_map.get(word_zh, [])
        if not isinstance(old, list):
            old = [old]
        added = False
        for w in word_answers:
            if w not in old:
                old.append(w)
                added = True
        if added:
            word_map[word_zh] = old
            lib[WORD_KEY] = word_map
        else:
            return
        _atomic_write(ANSWER_LIB_FILE, lib)


def lib_stats() -> dict:
    """返回各库条目数，供 Web 控制台展示。"""
    bank = bank_load()
    lib = _lib()
    words = lib.get(WORD_KEY, {})
    return {
        "bank": len(bank),
        "phrase": len([k for k in lib if k != WORD_KEY]),
        "word": len(words) if isinstance(words, dict) else 0,
        "pool": len(load_word_pool()),
        "dict": len(load_dict_zh()),
    }
