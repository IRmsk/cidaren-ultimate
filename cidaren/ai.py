# -*- coding: utf-8 -*-
"""
ai.py — LLM 答题客户端（OpenAI 兼容，融合 LangQi99 的 prompt 模板 + Cidaren-AI 的解析策略）
- mode 11/31/32/51/73 等定制 prompt
- 只取 choices[0].message.content（忽略 reasoning_content，修复 Cidaren-AI 的 bug#8）
- 超时重试，失败返回 None 由答题引擎兜底
"""
from __future__ import annotations

import json
import re
import uuid

import requests

from .config import as_int, get_runtime_config


def _chat_completions_url(llm_url: str) -> str:
    base = llm_url.rstrip("/")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _norm_options(opts) -> str:
    return "\n".join(f"{i}. {o.get('content', '')}" for i, o in enumerate(opts))


def build_prompt(topic: dict, word_defs: dict = None, task_words: list = None) -> str:
    """按题型构建 prompt（LangQi99 + Cidaren-AI 融合）。"""
    mode = topic.get("topic_mode")
    stem_obj = topic.get("stem") or {}
    stem = stem_obj.get("content", "")
    remark = stem_obj.get("remark", "") or ""
    opts = topic.get("options") or []
    opts_str = _norm_options(opts)

    wd_str = ""
    if word_defs:
        wd_lines = [f"  {w}: {'; '.join(ds)}" for w, ds in list(word_defs.items())[:40]]
        wd_str = "已知单词释义:\n" + "\n".join(wd_lines) + "\n\n"

    tw_str = ""
    if task_words:
        tw_str = f"\n本任务学习的单词（答案必在其中）: {', '.join(task_words)}"

    if mode == 32:
        return (
            f"{wd_str}题目: 用选项中的词组成短语, 中文含义是「{remark}」\n"
            f"空格数: {stem}\n选项:\n{opts_str}\n\n"
            "请选出正确的词并按正确顺序排列。只回答逗号分隔的选项内容(如: in,many,instances), 不要其他文字。"
        )
    if mode == 31:
        return (
            f"{wd_str}题目: 以下哪个是单词「{stem}」的正确释义?\n选项:\n{opts_str}\n\n"
            "只回答选项编号(如: 0), 不要其他文字。"
        )
    if mode == 11:
        rk = f"句子中文翻译(参考): {remark}\n" if remark else ""
        return (
            f"{wd_str}题目: 根据句意选择句中 {{}} 内单词的正确释义\n句子: {stem}\n{rk}选项:\n{opts_str}\n\n"
            "注意: 单词常有多个词义和词性, 必须结合上下文和中文翻译判断该词在此句中的具体含义。\n"
            "只回答选项编号(如: 0), 不要其他文字。"
        )
    if mode in (51, 52, 53, 54, 73):
        return (
            f"英语选词填空/拼写题。题目中 {{}} 或 _ 表示空格，请补全。\n\n"
            f"英文题目: {stem}\n中文含义: {remark}\n"
            f"选项: {opts_str if opts else '无'}{tw_str}\n\n"
            "只回答单词本身（多空格用逗号分隔），不要解释、不要标点。"
        )
    return (
        f"{wd_str}题目 (mode={mode}):\n题干: {stem}\n备注: {remark}\n选项:\n{opts_str}\n\n"
        "选择正确答案。如果是选择题回答编号(如: 0); 如果是组词题回答逗号分隔的词(如: in,many,instances)。不要其他文字。"
    )


def parse_llm_answer(ans_text: str, mode: int) -> int | str | list | None:
    """把 LLM 原始输出解析成可提交的答案（融合两家解析策略）。"""
    if not ans_text:
        return None
    ans_text = ans_text.strip()
    ans_text = re.sub(r'^[A-D][.、)\s]+', '', ans_text)
    ans_text = ans_text.strip('"\'`*')
    if "\n" in ans_text:
        lines = [l.strip() for l in ans_text.split("\n") if l.strip()]
        ans_text = lines[-1]

    if mode == 73:
        # 拼写题：尝试 JSON 数组
        try:
            arr = json.loads(ans_text)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr]
        except (ValueError, TypeError):
            pass
        # 逗号分隔
        if "," in ans_text:
            return [w.strip() for w in ans_text.split(",") if w.strip()]
        return ans_text
    if mode == 32:
        if "," in ans_text:
            return ans_text
        return ans_text
    # 选择题：数字编号
    m = re.search(r'\d+', ans_text)
    if m:
        return int(m.group())
    return ans_text


def llm_answer(topic: dict, word_defs: dict = None, task_words: list = None, config: dict = None):
    """调用 LLM 答题。失败返回 None（由引擎继续走空白试探/规则）。"""
    cfg = config or get_runtime_config()
    llm_url = (cfg.get("LLM_URL") or "").strip()
    llm_key = (cfg.get("LLM_KEY") or "").strip()
    llm_model = (cfg.get("LLM_MODEL") or "deepseek-chat").strip() or "deepseek-chat"
    max_tokens = as_int(cfg.get("LLM_MAX_TOKENS"), 1024)

    if not llm_url or not llm_key:
        return None

    mode = topic.get("topic_mode")
    prompt = build_prompt(topic, word_defs, task_words)
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {llm_key}",
    }
    data = {
        "model": llm_model,
        "messages": [
            {"role": "system",
             "content": f"[{uuid.uuid4()}] 你是英语词汇专家。请精准回答，只输出答案，不要解释。"},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.1,
    }
    url = _chat_completions_url(llm_url)

    last_err = None
    rate_hits = 0
    for attempt in range(4):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=(10, 60))
            if resp.status_code == 429:
                # LLM 限速：读 Retry-After，暂停后重试；连续多次则长暂停（YYY887 思路：限速不盲猜）
                rate_hits += 1
                import time as _t
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else (5 + rate_hits * 10)
                _t.sleep(wait)
                last_err = f"429 限速, 等待{wait}s (第{rate_hits}次)"
                continue
            resp.raise_for_status()
            if not resp.text.strip():
                last_err = "空响应"
                continue
            rj = resp.json()
            choices = rj.get("choices") or []
            if not choices:
                last_err = f"响应无 choices: {str(rj)[:200]}"
                continue
            content = (choices[0].get("message") or {}).get("content") or ""
            parsed = parse_llm_answer(content, mode)
            if parsed is None or parsed == "":
                last_err = "解析结果为空"
                continue
            return parsed
        except (requests.Timeout, requests.ConnectionError) as e:
            last_err = str(e)
            if attempt < 3:
                import time
                time.sleep(1.5 * (attempt + 1))
                continue
        except ValueError as e:
            last_err = f"非 JSON: {str(e)[:200]}"
            continue
        except Exception as e:
            last_err = str(e)
            continue
    return None
