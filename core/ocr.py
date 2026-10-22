# -*- coding: utf-8 -*-
"""
ocr.py — 图片题干/选项读图兜底（整合 xuhaoranbb-boop/vocabgo-auto 的读图思路）
三种模式（配置 OCR_MODE）：
  llm       — 用 OpenAI 兼容大模型视觉接口识别（如 deepseek-flash / gpt-4o / qwen-vl）
  tesseract — 本地 tesseract OCR（需安装 tesseract-ocr + pytesseract + Pillow）
  baidu     — 百度 OCR 通用文字识别（需 BAIDU_OCR_AK / BAIDU_OCR_SK）
失败返回 None，由答题引擎照常跳过。
"""
from __future__ import annotations

import json
import time

import requests

from core.config import as_int, get_runtime_config


def _chat_url(llm_url: str) -> str:
    base = llm_url.rstrip("/")
    return f"{base}/v1/chat/completions" if not base.endswith("/v1") else f"{base}/chat/completions"


def _ocr_llm(img_url: str, config: dict) -> str | None:
    """大模型视觉识别。"""
    llm_url = (config.get("LLM_URL") or "").strip()
    llm_key = (config.get("LLM_KEY") or "").strip()
    llm_model = (config.get("LLM_MODEL") or "deepseek-chat").strip() or "deepseek-chat"
    if not llm_url or not llm_key:
        return None
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {llm_key}"}
    data = {
        "model": llm_model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "识别这张图片中的所有文字，原样输出，不要解释。"},
                {"type": "image_url", "image_url": {"url": img_url}},
            ],
        }],
        "max_tokens": as_int(config.get("LLM_MAX_TOKENS"), 1024),
        "temperature": 0,
    }
    try:
        resp = requests.post(_chat_url(llm_url), headers=headers, json=data, timeout=(10, 60))
        resp.raise_for_status()
        choices = resp.json().get("choices") or []
        if not choices:
            return None
        text = (choices[0].get("message") or {}).get("content") or ""
        return text.strip() or None
    except Exception:
        return None


def _ocr_tesseract(img_url: str) -> str | None:
    """本地 tesseract OCR（依赖可选）。"""
    try:
        import io
        import pytesseract
        from PIL import Image
        r = requests.get(img_url, timeout=15)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content))
        text = pytesseract.image_to_string(img, lang="eng+chi_sim")
        return text.strip() or None
    except Exception:
        return None


def _baidu_token(ak: str, sk: str) -> str | None:
    try:
        resp = requests.post(
            "https://aip.baidubce.com/oauth/2.0/token",
            params={"grant_type": "client_credentials", "client_id": ak, "client_secret": sk},
            timeout=15,
        )
        return (resp.json() or {}).get("access_token")
    except Exception:
        return None


def _ocr_baidu(img_url: str, config: dict) -> str | None:
    """百度 OCR 通用文字识别（标准版免费）。"""
    ak = (config.get("BAIDU_OCR_AK") or "").strip()
    sk = (config.get("BAIDU_OCR_SK") or "").strip()
    if not ak or not sk:
        return None
    token = _baidu_token(ak, sk)
    if not token:
        return None
    try:
        img_resp = requests.get(img_url, timeout=15)
        img_resp.raise_for_status()
        resp = requests.post(
            "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic",
            params={"access_token": token},
            data={"image": img_resp.content},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=20,
        )
        words = [(w or {}).get("words", "") for w in (resp.json() or {}).get("words_result", [])]
        return "\n".join(w for w in words if w) or None
    except Exception:
        return None


def ocr_image(img_url: str, config: dict = None) -> str | None:
    """按 OCR_MODE 识别图片文字，失败返回 None。"""
    cfg = config or get_runtime_config()
    mode = (cfg.get("OCR_MODE") or "off").strip().lower()
    if mode == "llm":
        return _ocr_llm(img_url, cfg)
    if mode == "tesseract":
        return _ocr_tesseract(img_url)
    if mode == "baidu":
        return _ocr_baidu(img_url, cfg)
    return None


def topic_ocr(topic: dict, config: dict, log=lambda s: None) -> bool:
    """
    对题目做读图：把图片题干/图片选项识别成文字并回填。
    返回是否发生过 OCR（True 表示有图被处理）。
    """
    if (config.get("OCR_MODE") or "off").strip().lower() == "off":
        return False
    changed = False
    stem = topic.get("stem") or {}
    if isinstance(stem, dict):
        img = stem.get("img")
        if img and not (stem.get("content") or "").strip():
            text = ocr_image(img, config)
            if text:
                stem["content"] = text
                log(f"  [OCR] 题干识别: {text[:40]}")
                changed = True
        # 备注也可能是图片
        remark_img = stem.get("remark_img") or stem.get("img_remark")
        if remark_img and not stem.get("remark"):
            text = ocr_image(remark_img, config)
            if text:
                stem["remark"] = text
                changed = True
    for opt in topic.get("options") or []:
        if not isinstance(opt, dict):
            continue
        if not (opt.get("content") or "").strip() and opt.get("img"):
            text = ocr_image(opt["img"], config)
            if text:
                opt["content"] = text.strip()
                log(f"  [OCR] 选项识别: {text[:40]}")
                changed = True
    return changed
