# -*- coding: utf-8 -*-
"""
solver.py — 全题型答题引擎（整合 easy_cidaren 规则匹配 + Cidaren-AI 空白试探/AI优先 + LangQi99 题库缓存/LLM兜底）

答案来源优先级（可配置）：
  题库指纹缓存 -> 短语/单词答案库 -> 规则匹配(查词比对) -> [AI 优先] -> 空白试探(服务端纠错) -> [LLM 兜底] -> 跳过
判错后自动把 answer_corrects 学习进本地答案库，越用越准。

支持题型：0 / 11 / 13 / 15 / 16 / 17 / 18 / 21 / 22 / 31 / 32 / 41 / 42 / 43 / 44 / 51 / 52 / 53 / 54 / 73
"""
from __future__ import annotations

import json
import random
import re
import time
import urllib.parse

from ..core import bank as bk
from ..core import ocr as ocr_mod
from . import report as rp
from ..core.ai import llm_answer
from ..core.config import as_bool, as_int, get_runtime_config

_WORD_PAT = re.compile(r'\{(\w+)\}')

# 选择题类 mode：LLM/AI 返回选项编号
CHOICE_MODES = (11, 13, 15, 16, 17, 18, 21, 22, 41, 42, 43, 44)


# ============ 工具函数 ============

def _norm(s: str) -> str:
    return re.sub(r'\s+', '', s or "").lower()


def _norm_word(w: str) -> str:
    """去词首尾标点（保留内部连字符/撇号）。"""
    return (w or "").strip(' \t,;:.()[]"\'，。、；：')


def _delete_other_char(s: str) -> str:
    """只保留英文字母/空格/连字符/撇号。"""
    return re.sub(r'[^A-Za-z\s\'\-]', '', s or "").strip()


def _template_slots(content: str):
    """mode 32 模板空位解析：'_'=1 词位，'_-_'=2 词位。返回 (空位token列表, 总词位数)。"""
    slots, total = [], 0
    for m in re.finditer(r'_-_|_', content or ""):
        tok = m.group(0)
        slots.append(tok)
        total += 1 if tok == "_" else 2
    return slots, total


def _topic_key(t: dict) -> str:
    """题目指纹（LangQi99 思路）：mode + 题干 + 备注 + 选项前20字。"""
    mode = t.get("topic_mode", "?")
    stem = (t.get("stem") or {}).get("content", "")
    remark = (t.get("stem") or {}).get("remark", "") or ""
    if isinstance(remark, list):
        remark = json.dumps(remark, ensure_ascii=False, sort_keys=True)
    opts = "|".join((o.get("content") or "")[:20] for o in (t.get("options") or []))
    sn = re.sub(r'\s+', ' ', stem or "").strip().lower()
    kind = "coll" if _is_collocation(t) else "norm"
    return f"{mode}::{kind}::{sn}::{remark}::{opts.lower()}"


def _is_collocation(topic: dict) -> bool:
    """搭配题：mode=31 且 stem.remark 是 dict 列表（含 relation 字段）。"""
    if topic.get("topic_mode") != 31:
        return False
    rk = (topic.get("stem") or {}).get("remark")
    return isinstance(rk, list) and len(rk) > 0 and isinstance(rk[0], dict)


def _opts(topic: dict) -> list:
    return topic.get("options") or []


def _opt_contents(topic: dict) -> list:
    return [o.get("content", "") for o in _opts(topic)]


def _filler_option(ctx, topic: dict) -> tuple:
    """选项排序优化（easy 思路）：词表内的词排前面。返回 (排序后选项, 原始顺序)。"""
    options, source = [], []
    word_list = ctx.word_list
    for opt in _opts(topic):
        content = opt.get("content", "")
        source.append(opt)
        if content in word_list:
            options.insert(0, opt)
        else:
            options.append(opt)
    return options, source


def _query_means(ctx, word: str) -> list | None:
    """查询单词释义列表：学习卡片缓存 -> 离线词典（ECDICT 开源数据）-> 查词接口。

    离线词典层插在缓存之后、查词接口之前：常见考试词离线直接命中，
    USE_WORD_API=0 的弱网环境也能查词，减少对网络的依赖。
    """
    if not word:
        return None
    w = word.lower()
    defs = ctx.word_defs.get(w)
    if defs:
        return defs
    # 离线释义词典（ECDICT 开源数据，MIT 协议）
    offline = bk.load_dict_zh().get(w)
    if offline:
        ctx.word_defs[w] = offline
        return offline
    # 弱网可关查词接口（USE_WORD_API=0），直接走 AI/空白试探
    if not as_bool(ctx.config.get("USE_WORD_API"), True):
        return None
    if not ctx.course_id or not ctx.now_unit:
        return None
    resp = ctx.client.query_word(ctx.course_id, ctx.now_unit, word)
    ctx._last_query_resp = resp  # 供例句匹配使用
    data = resp.get("data") or {}
    means = []
    try:
        if isinstance(data, dict) and data.get("options"):
            for m in data["options"]:
                c = (m.get("content") or {}).get("mean", "")
                if c:
                    means.append(re.sub(r"（.*）", "", c))
        elif isinstance(data, dict) and data.get("means"):
            for m in data["means"]:
                mean = m.get("mean")
                if isinstance(mean, list):
                    means.append(" ".join(mean))
                elif mean:
                    means.append(str(mean))
    except Exception:
        pass
    if means:
        ctx.word_defs[w] = means
    return means or None


def _word_revert_api(ctx, word: str) -> str:
    """把单词转原型（用官方 SearchWord 接口，替代 easy 的 spaCy 依赖）。弱网可跳过。"""
    if as_bool(ctx.config.get("USE_WORD_API"), True):
        try:
            resp = ctx.client.search_word(word)
            data = resp.get("data") or {}
            meaning = (data.get("word_mean") or {}).get("meaning") or ""
            if meaning:
                decoded = bytes(meaning, "utf-8").decode("unicode_escape")
                m = re.search(r'<span>(.+?)</span>', decoded)
                if m:
                    return m.group(1)
        except Exception:
            pass
    # 简单后缀规则兜底
    for suffix, repl in (("ies", "y"), ("es", ""), ("s", ""), ("ed", ""), ("ing", "")):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[:-len(suffix)] + repl
    return word


def _match_phrase_by_word_zh(ctx, word_zh: str) -> str | None:
    """按中文释义反查词表中的英文短语（easy 思路，术语课程用）。兼容 word_pool 的 URL 编码值。"""
    word_map = ctx.word_dict or {}
    if not word_map:
        return None
    if word_zh in word_map:
        return urllib.parse.unquote(word_map[word_zh])
    pattern = re.compile(r'[\s（）()“”"\'‘’《》]')
    nm = pattern.sub("", word_zh)
    for zh, en in word_map.items():
        if pattern.sub("", zh) == nm:
            return urllib.parse.unquote(en)
    return None


def _build_phrase_answer(ctx, topic: dict, phrase: str) -> str:
    """mode 32 汉译英填空：按 options 单词池过滤固定词 + 模板空位对齐提交（easy 核心）。"""
    options = [o.get("content") for o in _opts(topic) if isinstance(o, dict)]
    if not options:
        return _delete_other_char(phrase)
    opt_set = {o.lower() for o in options if isinstance(o, str)}
    tokens = phrase.split()
    cand = []
    i = 0
    while i < len(tokens):
        w = tokens[i]
        matched = False
        for k in range(3, 1, -1):
            if i + k <= len(tokens):
                seg = " ".join(tokens[i:i + k])
                if seg.lower() in opt_set:
                    cand.append(seg)
                    i += k
                    matched = True
                    break
        if matched:
            continue
        nw = _norm_word(w)
        if w.lower() in opt_set:
            cand.append(w)
            i += 1
            continue
        if nw.lower() in opt_set:
            cand.append(nw)
            i += 1
            continue
        if "-" in w:
            parts = [p.lower() for p in w.split("-") if p]
            if parts and all(p in opt_set for p in parts):
                cand.append(w)
                i += 1
                continue
        i += 1
    if not cand:
        return _delete_other_char(phrase)
    content = (topic.get("stem") or {}).get("content", "") if isinstance(topic, dict) else ""
    slots, slot_words = _template_slots(content)
    if slot_words:
        result = []
        ci = 0
        for slot in slots:
            if ci >= len(cand):
                break
            w = cand[ci]
            if slot == "_-_" and "-" in w:
                parts = w.split("-")
                if all(p.lower() in opt_set for p in parts):
                    result.extend(parts)
                    ci += 1
                    continue
            result.append(w)
            ci += 1
        if len(result) == slot_words:
            picked = result
        else:
            picked = [p for w in cand for p in (w.split("-") if "-" in w else [w])]
            picked = [p for p in picked if p.lower() in opt_set]
    else:
        picked = cand
    if not picked:
        return _delete_other_char(phrase)
    return ",".join(picked)


def _match_tips_words(tips, word_lens, words, strict_len=True, exclude=None):
    """mode 73 前缀匹配（easy 思路）。"""
    exclude = exclude or set()
    answers = []
    for i, tip in enumerate(tips):
        wlen = word_lens[i] if i < len(word_lens) else None
        matched = None
        for w in words:
            if w.lower() in exclude:
                continue
            if w.lower().startswith(tip.lower()) and (not strict_len or not wlen or len(w) == int(wlen)):
                matched = w
                break
        if matched is None:
            return None
        answers.append(matched)
    return answers


def _candidate_words(ctx, remark: str) -> list | None:
    """mode 73 候选词池合并（easy 思路）：短语库 -> 单词库(变体) -> 词表短语。"""
    pool = []
    lib = bk.lookup_phrase(remark)
    if lib:
        pool += [w for w in re.split(r'[\s\-(),]+', lib[0]) if w]
    words = bk.lookup_word(remark)
    if words:
        pool += list(words)
    phrase = _match_phrase_by_word_zh(ctx, remark)
    if phrase:
        pool += [w for w in re.split(r'[\s\-(),/]+', phrase) if w]
    return list(dict.fromkeys(pool)) or None


def _global_word_pool(ctx) -> list | None:
    """全局词表单词池（easy 思路）：跨词条兜底。"""
    word_map = ctx.word_dict or {}
    if not word_map:
        return None
    pool = []
    for en in word_map.values():
        pool += [w for w in re.split(r'[\s\-(),/]+', en) if w]
    return list(dict.fromkeys(pool)) or None


# ============ 规则匹配（easy_cidaren 移植） ============

def rule_answer(ctx, topic: dict):
    """按题型规则匹配答案；答不上返回 None（交给 AI/空白试探）。"""
    mode = topic.get("topic_mode")
    stem_obj = topic.get("stem") or {}
    stem = stem_obj.get("content", "")
    remark = stem_obj.get("remark", "") or ""

    if mode == 11 or mode in (15, 16, 21, 22):
        # 英译汉：题干 {word} 或直接单词 -> 查词 -> 比对选项释义
        return _word_form_mean(ctx, topic)
    if mode == 17 or mode == 18:
        # 看义选词
        return _mean_to_word(ctx, topic)
    if mode == 31:
        # 搭配题（循环 verify 在引擎层处理，这里给候选 tag 列表）
        if _is_collocation(topic):
            return _together_word_tags(topic)
        return None
    if mode == 32:
        return _select_word(ctx, topic)
    if mode in (41, 42, 43, 44):
        return _full_sentence(ctx, topic)
    if mode in (51, 52, 53, 54):
        return _complete_sentence(ctx, topic)
    if mode == 73:
        return _complete_spelling(ctx, topic)
    if mode == 13:
        # 例句配对：规则无解，交 AI/空白试探
        return None
    return None


def _word_form_mean(ctx, topic: dict):
    """英译汉：提取 {word} -> 转原型 -> 查词 -> select_mean。"""
    exam = (topic.get("stem") or {}).get("content", "").replace(" ", "")
    m = re.findall(r"{(.*?)}", exam)
    word = m[0] if m else exam
    if word not in ctx.word_list:
        if word.endswith("ed") and word[:-2] in ctx.word_list:
            word = word[:-2]
        elif word.endswith("ing") and word[:-3] in ctx.word_list:
            word = word[:-3]
        else:
            word = _word_revert_api(ctx, word)
    means = _query_means(ctx, word)
    if not means:
        return None
    options = [o.get("content", "") for o in _opts(topic)]
    for index, option in enumerate(options):
        for mean in means:
            if sorted(option.replace(" ", "")) == sorted(mean.replace(" ", "")) or mean in option:
                return index
    return None


def _mean_to_word(ctx, topic: dict):
    """看义选词：遍历选项查词，比对释义是否等于题干中文。"""
    word_mean = (topic.get("stem") or {}).get("content", "")
    for opt in _opts(topic):
        word = opt.get("content", "")
        if word not in ctx.word_list:
            rw = _word_revert_api(ctx, word)
            if rw not in ctx.word_list:
                continue
            word = rw
        means = _query_means(ctx, word)
        if not means:
            continue
        for mean in means:
            if sorted(word_mean.replace(" ", "")) == sorted(mean.replace(" ", "")):
                return opt.get("answer_tag", 0)
    return None


def _together_word_tags(topic: dict):
    """搭配题：stem.remark 中所有 relation 直接命中选项（easy 思路，返回候选 tag 列表）。"""
    relations = set()
    for r in (topic.get("stem") or {}).get("remark", []):
        if isinstance(r, dict):
            rel = (r.get("relation") or "").strip().lower()
            if rel:
                relations.add(rel)
    if not relations:
        return None
    tags = []
    for opt in _opts(topic):
        if (opt.get("content") or "").strip().lower() in relations:
            tags.append(opt.get("answer_tag"))
    return tags or None


def _select_word(ctx, topic: dict):
    """mode 32 汉译英填空：答案库 -> 词表短语反查 -> 模板空位对齐。"""
    word_mean = (topic.get("stem") or {}).get("remark", "")
    lib = bk.lookup_phrase(word_mean)
    if lib:
        return _build_phrase_answer(ctx, topic, lib[0])
    phrase = _match_phrase_by_word_zh(ctx, word_mean)
    if phrase:
        return _build_phrase_answer(ctx, topic, phrase)
    return None


def _full_sentence(ctx, topic: dict):
    """mode 41-44 选词完成句子：单词库命中 -> 例句匹配。"""
    remark = (topic.get("stem") or {}).get("remark") if isinstance(topic, dict) else None
    word_answers = bk.lookup_word(remark)
    if word_answers:
        content = (topic.get("stem") or {}).get("content", "")
        fixed = {w.lower() for w in re.findall(r'[A-Za-z]+',
                                               content.replace("{}", " ").replace("{", " ").replace("}", " "))}
        lower_words = [w.lower() for w in word_answers if w.lower() not in fixed]
        if not lower_words:
            lower_words = [w.lower() for w in word_answers]
        for opt in _opts(topic):
            if isinstance(opt, dict) and opt.get("content", "").lower() in lower_words:
                return opt.get("answer_tag")
    # 例句匹配（easy word_examples 思路）
    exam_zh = remark
    for opt in _opts(topic):
        word = opt.get("content", "")
        if word not in ctx.word_list:
            rw = _word_revert_api(ctx, word)
            if rw not in ctx.word_list:
                continue
            word = rw
        means = _query_means(ctx, word)
        if not means:
            continue
        found = _find_example_word(ctx, word, exam_zh)
        if found:
            tag = opt.get("answer_tag")
            if isinstance(tag, str):
                return tag + "0"
            return tag
    return None


def _find_example_word(ctx, word: str, exam_zh: str) -> str | None:
    """在查词结果的例句中找中文翻译匹配的那一句的 {单词}（easy word_examples）。"""
    resp = getattr(ctx, "_last_query_resp", None)
    data = (resp or {}).get("data") or {}
    try:
        if isinstance(data, dict) and data.get("options"):
            for contents in data["options"]:
                for example in (contents.get("content") or {}).get("example", []):
                    if example.get("sen_mean_cn") == exam_zh:
                        m = re.findall(r'{(.+?)}', example.get("sen_content", ""))
                        if m:
                            return m[0]
        elif isinstance(data, dict) and data.get("means"):
            for means in data["means"]:
                for usage in means.get("usages", []):
                    for sentence in usage.get("examples", []):
                        if sentence.get("sen_mean_cn") == exam_zh:
                            m = re.findall(r'{(.+?)}', sentence.get("sen_content", ""))
                            if m:
                                return m[0]
    except Exception:
        pass
    return None


def _infer_lens_from_stem(content: str) -> list | None:
    """从题干文本推断每空字母数：数下划线占位符。

    场景：服务器未给 w_lens（OCR 读图、mock、异常数据）时，题干里
    通常有 `_ _ _ _` / `____` / `＿＿` 这类空位占位，数出下划线个数
    就是目标单词的字母数，据此可严格筛选候选答案。
    """
    if not content:
        return None
    # 归一化：全角下划线 -> 半角；把空格分隔的下划线组 `_ _ _` 合并成等量连续下划线 `___`
    t = re.sub(r'＿+', lambda m: '_' * len(m.group(0)), content)
    t = re.sub(r'_(?:\s+_)+', lambda m: '_' * m.group(0).count('_'), t)
    runs = re.findall(r'_+', t)
    if not runs:
        return None
    # 过滤明显噪声：超长 run（OCR 把分隔线/破折号当下划线）
    lens = [len(r) for r in runs if 1 <= len(r) <= 15]
    return lens or None


def _complete_sentence(ctx, topic: dict):
    """mode 51-54 补全单词：按 w_len + w_tip 在词表中匹配。

    长度来源：服务器 w_lens/w_len 优先；缺失时从题干下划线占位推断。
    """
    w_lens = topic.get("w_lens") or []
    word_len = w_lens[0] if w_lens else topic.get("w_len") or 0
    if not word_len:
        # 服务器没给长度 -> 数题干下划线推断
        content = (topic.get("stem") or {}).get("content", "")
        lens = _infer_lens_from_stem(content)
        word_len = lens[0] if lens else 0
    w_tip = (topic.get("w_tip") or "").lower()
    for word in ctx.word_list:
        if word.lower().startswith(w_tip):
            if len(word) == word_len:
                return word
            if len(word) + 1 == word_len:
                return word + "s"
    return None


def _complete_spelling(ctx, topic: dict):
    """mode 73 补全拼写：5 级候选池（easy 核心）。

    每空字母数：服务器 w_lens 优先；缺失时从题干下划线占位推断，
    让拼写题在没有长度信息时也能按字母数筛选候选。
    """
    stem_obj = topic.get("stem") or {}
    content = stem_obj.get("content", "")
    remark = stem_obj.get("remark", "")
    tips = re.findall(r'\{(\w+)\}', content)
    w_lens = topic.get("w_lens") or _infer_lens_from_stem(content) or []
    fixed = {w.lower() for w in re.findall(r'[A-Za-z]+', re.sub(r'\{[^}]*\}', ' ', content))}
    if not tips:
        slot_count = content.count("{}")
        options = [o.get("content") for o in _opts(topic) if isinstance(o, dict)]
        opt_lower = [o.lower() for o in options if isinstance(o, str)]
        candidates = _candidate_words(ctx, remark)
        if candidates:
            picked = [w for w in candidates if w.lower() in opt_lower]
            if not picked and w_lens:
                # 无选项直命中时，用推断长度过滤候选（严格 -> 宽松）
                picked = [w for w in candidates
                          if not w_lens or len(w) == w_lens[0]]
                if not picked:
                    picked = candidates
            if picked:
                return json.dumps(picked[:slot_count] if slot_count else picked)
        return None
    # 1. 短语库
    lib = bk.lookup_phrase(remark)
    if lib:
        words = [w for w in re.split(r'[\s\-(),/]+', lib[0]) if w]
        ans = _match_tips_words(tips, w_lens, words, exclude=fixed)
        if ans:
            return json.dumps(ans)
    # 2. 单词库变体
    word_answers = bk.lookup_word(remark)
    if word_answers:
        ans = _match_tips_words(tips, w_lens, word_answers, strict_len=False, exclude=fixed)
        if ans:
            return json.dumps(ans)
    # 3. 词表短语
    phrase = _match_phrase_by_word_zh(ctx, remark)
    if phrase:
        words = phrase.split()
        ans = _match_tips_words(tips, w_lens, words, exclude=fixed)
        if ans:
            return json.dumps(ans)
    # 4. 合并候选池（先严格后宽松）
    candidates = _candidate_words(ctx, remark)
    if candidates:
        ans = _match_tips_words(tips, w_lens, candidates, exclude=fixed)
        if ans:
            return json.dumps(ans)
        ans = _match_tips_words(tips, w_lens, candidates, strict_len=False, exclude=fixed)
        if ans:
            return json.dumps(ans)
    # 5. 全局词表池
    gpool = _global_word_pool(ctx)
    if gpool:
        ans = _match_tips_words(tips, w_lens, gpool, exclude=fixed)
        if ans:
            return json.dumps(ans)
        ans = _match_tips_words(tips, w_lens, gpool, strict_len=False, exclude=fixed)
        if ans:
            return json.dumps(ans)
    return None


# ============ 提交验证（Cidaren-AI / LangQi99 融合） ============

class TopicContext:
    def __init__(self, client, config=None, task_kind="class", course_id=None, list_id=None,
                 task_type=1, grade=2, release_id=None):
        self.client = client
        self.config = config or get_runtime_config()
        self.task_kind = task_kind  # "class" | "study"
        self.course_id = course_id
        self.list_id = list_id
        self.task_type = task_type
        self.grade = grade
        self.release_id = release_id
        self.now_unit = None          # 当前单元 list_id（查词用）
        self.word_list = []           # 当前单元单词（easy 判词依赖）
        self.word_dict = {}           # word_zh -> word（跨单元累积，mode 73 全局池）
        self.word_defs = {}           # word -> [释义]（学习卡片 + 查词缓存）
        self.stats = {"ok": 0, "fail": 0, "wrong": 0, "skip": 0, "cache": 0, "rule": 0, "ai": 0, "blank": 0}

    @property
    def is_study(self) -> bool:
        return self.task_kind == "study"

    def delay(self, lo=1.5, hi=3.5):
        time.sleep(random.uniform(lo, hi))


def _verify(ctx, topic_code, answer):
    if ctx.is_study:
        return ctx.client.study_verify(topic_code, answer)
    return ctx.client.verify(topic_code, answer)


def _submit_next(ctx, topic_code, time_spent):
    if ctx.is_study:
        return ctx.client.study_submit(topic_code, time_spent)
    return ctx.client.submit(topic_code, time_spent)


def _learn_from_corrects(ctx, topic: dict, corrects):
    """判错后自学习（easy 思路）：mode 32 入短语库，mode 42/73 入单词库。"""
    mode = topic.get("topic_mode")
    word_zh = (topic.get("stem") or {}).get("remark", "")
    if not word_zh or not corrects:
        return
    if mode == 32:
        # 统一存空格分隔的完整短语（answer_lib 格式），方便 _build_phrase_answer 复用
        if isinstance(corrects, list):
            phrase = " ".join(str(c) for c in corrects if isinstance(c, str))
            if phrase:
                bk.add_phrase(word_zh, [phrase])
        else:
            bk.add_phrase(word_zh, [str(corrects)])
    elif mode in (42, 73):
        opts = _opts(topic)
        words = []
        for idx in corrects:
            if isinstance(idx, int) and 0 <= idx < len(opts):
                c = opts[idx].get("content")
                if c:
                    words.append(c)
        if words:
            bk.add_words(word_zh, words)


def _learn_from_success(ctx, topic: dict, answer, mode, src):
    """
    AI/规则答对后自动上传题库（用户要求：没见过的题 AI 答对就入库）。
    - 题目指纹 bank 由调用方统一写入
    - mode 32 答对：把逗号分隔的答案转空格分隔，写入短语库
    - mode 73 答对：AI 返回的 JSON 单词数组写入单词库
    """
    word_zh = (topic.get("stem") or {}).get("remark", "")
    if not word_zh:
        return
    if mode == 32 and isinstance(answer, str):
        phrase = answer.replace(",", " ")
        if phrase:
            bk.add_phrase(word_zh, [phrase])
    elif mode == 73:
        words = []
        if isinstance(answer, list):
            words = [str(w) for w in answer if isinstance(w, str)]
        elif isinstance(answer, str):
            try:
                arr = json.loads(answer)
                if isinstance(arr, list):
                    words = [str(w) for w in arr]
            except (ValueError, TypeError):
                words = [w.strip() for w in answer.split(",") if w.strip()]
        if words:
            bk.add_words(word_zh, words)


def _report(ctx, mode, src, ok, stem="", remark="", answer=""):
    """写一条测试报告（task 维度聚合）。"""
    try:
        rp.report_add(rp.task_key(ctx.task_kind, getattr(ctx, "task_id", "?"),
                                  getattr(ctx, "release_id", "?")),
                      mode, src, ok, stem, remark, answer)
    except Exception:
        pass


def _norm_corrects_answer(corrects, mode, stem) -> str:
    """把服务端 answer_corrects 统一成可提交的答案格式（easy + cidaren_ai 融合）。"""
    final_ans = corrects[0] if isinstance(corrects, list) else corrects
    if mode == 32 and isinstance(final_ans, str):
        words = final_ans.split()
        blank_count = stem.count("_")
        if len(words) < blank_count:
            expanded = []
            for w in words:
                expanded.extend(w.split("-") if "-" in w else [w])
            words = expanded
        return ",".join(words) if len(words) > 1 else final_ans
    if mode == 73 and isinstance(corrects, list):
        return json.dumps([str(c) for c in corrects])
    return final_ans


def _blank_probe(ctx, topic, key, stem, code, mode, logf):
    """
    空白试探（Cidaren-AI 思路）：题库/AI/规则都答不上时，提交空答案让服务端返回标准答案。
    不盲猜——拿不到标准答案就跳过，宁缺毋滥。
    """
    blank = "" if mode != 73 else json.dumps([""] * max(stem.count("{"), 1))
    resp0 = _verify(ctx, code, blank)
    d0 = resp0.get("data") or {}
    new_code = d0.get("topic_code", code)
    if resp0.get("code") == 1 and d0.get("answer_result") == 1:
        ctx.stats["ok"] += 1
        ctx.stats["blank"] += 1
        _report(ctx, mode, "blank", 1, stem, "", "")
        logf("OK", "空白试探直接命中", " [blank]")
        return _submit_next(ctx, new_code, random.randint(8000, 12000))
    c2 = d0.get("answer_corrects") or []
    if c2:
        ctx.stats["blank"] += 1
        _learn_from_corrects(ctx, topic, c2)
        ans2 = _norm_corrects_answer(c2, mode, stem)
        bk.bank_add(key, ans2, stem)
        rv = _verify(ctx, new_code, ans2)
        dv = rv.get("data") or {}
        if dv.get("answer_result") == 1:
            ctx.stats["ok"] += 1
            _report(ctx, mode, "blank", 1, stem, "", ans2)
            logf("OK", _disp_answer(topic, ans2, mode), " [blank+fix]")
            return _submit_next(ctx, dv.get("topic_code", new_code), random.randint(8000, 12000))
        ctx.stats["fail"] += 1
        _report(ctx, mode, "skip", 0, stem, "", "")
        logf("SKIP", f"纠错仍失败: {((topic.get('stem') or {}).get('remark') or stem)[:34]}", " [skip]")
        return _submit_next(ctx, dv.get("topic_code", new_code), 20000)

    ctx.stats["skip"] += 1
    ctx.stats["fail"] += 1
    _report(ctx, mode, "skip", 0, stem, "", "")
    logf("SKIP", f"无解跳过: {((topic.get('stem') or {}).get('remark') or stem)[:34]}", " [skip]")
    return _submit_next(ctx, code, 20000)


def solve_topic(ctx: TopicContext, topic: dict, log=lambda s: None):
    """
    处理一道题。返回下一题响应 dict（含 topic_code / topic_done_num）或 None（任务结束）。
    """
    mode = topic.get("topic_mode")
    stem_obj = topic.get("stem") or {}
    stem = stem_obj.get("content", "?")
    remark = stem_obj.get("remark", "") or ""
    key = _topic_key(topic)
    opts = _opts(topic)
    ai_priority = as_bool(ctx.config.get("AI_PRIORITY"), True)
    min_t = as_int(ctx.config.get("DELAY_PER_QUESTION"), 2) or 2

    def logf(tag, text, extra=""):
        log(f"  [{mode:>2}] {tag} {str(text)[:42]}{extra}")

    # ---------- OCR 读图兜底（图片题干/选项 -> 文字） ----------
    try:
        ocr_mod.topic_ocr(topic, ctx.config, log=log)
    except Exception:
        pass

    # ---------- mode 0：学习卡片，收集释义并跳过 ----------
    if mode == 0:
        word = stem.strip().lower()
        defs = [o.get("content", "") for o in opts if o.get("content")]
        if defs:
            ctx.word_defs[word] = defs
        _report(ctx, mode, "card", 0, stem, remark, "")
        resp = _submit_next(ctx, topic["topic_code"], random.randint(1000, 2500))
        return resp

    # ---------- 搭配题 mode 31（多选，循环 verify） ----------
    if _is_collocation(topic):
        return _solve_collocation(ctx, topic, log, logf)

    # ---------- 常规题 ----------
    code = topic["topic_code"]
    answer = None
    src = None

    # 1. 题库指纹缓存（新格式 + 旧格式双查）
    cached = bk.bank_lookup(key, ctx.bank)
    if cached is not None:
        answer = list(cached) if isinstance(cached, list) else cached
        src = "cache"
        ctx.stats["cache"] += 1

    # 2. AI 优先
    if answer is None and ai_priority:
        answer = llm_answer(topic, ctx.word_defs, ctx.word_list, ctx.config)
        if answer is not None:
            src = "ai"
            ctx.stats["ai"] += 1

    # 3. 规则匹配
    if answer is None:
        answer = rule_answer(ctx, topic)
        if answer is not None:
            src = "rule"
            ctx.stats["rule"] += 1

    # 4. LLM 兜底（规则优先模式）
    if answer is None and not ai_priority:
        answer = llm_answer(topic, ctx.word_defs, ctx.word_list, ctx.config)
        if answer is not None:
            src = "llm"
            ctx.stats["ai"] += 1

    # 5. 题库/AI/规则都答不上：AI 二次兜底（首次可能因超时/限速失败），不行转空白试探
    if answer is None and ai_priority:
        answer2 = llm_answer(topic, ctx.word_defs, ctx.word_list, ctx.config)
        if answer2 is not None:
            answer, src = answer2, "ai"
            ctx.stats["ai"] += 1

    if answer is not None:
        logf("?", "答题", f" [{src}]")
        resp, need_probe = _commit_answer(ctx, topic, key, stem, remark, code, answer, src, mode, logf)
        if resp is not None:
            return resp
        if need_probe:
            # 首次判错且服务端没给纠错：AI 再组合一次（rule!wrong 是 32 型丢分主因，
            # 首次 AI/规则失败往往是超时或词表反查错，AI 带选项词池二次尝试能救回一部分）
            answer2 = llm_answer(topic, ctx.word_defs, ctx.word_list, ctx.config)
            if answer2 is not None and str(answer2) != str(answer):
                logf("?", "AI 二次组合", " [ai]")
                resp2, need2 = _commit_answer(ctx, topic, key, stem, remark, code, answer2, "ai", mode, logf)
                if resp2 is not None:
                    return resp2
            _report(ctx, mode, src + "!wrong", 0, stem, remark, answer)
            logf("?", "仍无解，转空白试探", " [probe]")
            return _blank_probe(ctx, topic, key, stem, code, mode, logf)
        return resp

    logf("?", "题库/AI/规则均无解，转空白试探", " [probe]")
    return _blank_probe(ctx, topic, key, stem, code, mode, logf)


def _commit_answer(ctx, topic, key, stem, remark, code, answer, src, mode, logf):
    """验证并提交答案：OK 记分入库 / FIX 用服务端纠错答案补交。
    返回 (resp, need_probe)：resp=下一题响应或 None；need_probe=True 表示判错且无纠错。
    """
    code, ar, corrects, over = _verify_and_learn(ctx, topic, code, answer, mode, logf)
    if ar == 1:
        ctx.stats["ok"] += 1
        bk.bank_add(key, answer, stem)
        if src not in ("cache", "blank"):
            _learn_from_success(ctx, topic, answer, mode, src)
        _report(ctx, mode, src, 1, stem, remark, answer)
        logf("OK", _disp_answer(topic, answer, mode), f" [{src}]")
        return _submit_next(ctx, code, random.randint(8000, 12000)), False
    if corrects:
        final_ans = _norm_corrects_answer(corrects, mode, stem)
        bk.bank_add(key, final_ans, stem)
        _report(ctx, mode, "fix", 1, stem, remark, final_ans)
        logf("FIX", _disp_answer(topic, final_ans, mode), " [corrects]")
        return _submit_next(ctx, code, random.randint(8000, 12000)), False
    return None, True


def _solve_collocation(ctx, topic, log, logf):
    """搭配题：候选 tag 列表逐个 verify，直到 over_status=1（LangQi99 思路）。"""
    opts = _opts(topic)
    answer_num = topic.get("answer_num") or 2
    key = _topic_key(topic)
    tags, src = None, None
    cached = bk.bank_lookup(key, ctx.bank)
    if isinstance(cached, list) and cached:
        tags, src = list(cached), "cache"
    if not tags:
        tags = _together_word_tags(topic)
        if tags:
            src = "match"
    if not tags:
        a_one = llm_answer(topic, ctx.word_defs, ctx.word_list, ctx.config)
        if isinstance(a_one, int):
            tags, src = [a_one], "ai"
    if not tags:
        tags, src = [0], "guess"

    chosen, ok_tags = [], []
    cur_code = topic["topic_code"]
    for ans_tag in tags[:answer_num]:
        if ans_tag in chosen:
            continue
        vr = _verify(ctx, cur_code, ans_tag)
        vd = vr.get("data") or {}
        cur_code = vd.get("topic_code", cur_code)
        if vd.get("answer_result") == 1:
            ok_tags.append(ans_tag)
        chosen.append(ans_tag)
        cs = vd.get("answer_corrects") or []
        if cs:
            for c in cs:
                if c not in chosen and c not in ok_tags:
                    ok_tags.append(c)
        if vd.get("over_status") == 1:
            break
        ctx.delay(0.5, 1.0)

    if ok_tags:
        bk.bank_add(key, ok_tags, topic.get("stem", {}).get("content", ""))
        ctx.stats["ok"] += 1
        _report(ctx, 31, src or "match", 1, topic.get("stem", {}).get("content", ""), "", ",".join(str(t) for t in ok_tags))
        logf("OK", ",".join(_disp_answer(topic, t, 31) for t in (ok_tags or chosen)), f" [{src}]")
    else:
        ctx.stats["skip"] += 1
        _report(ctx, 31, "skip", 0, topic.get("stem", {}).get("content", ""), "", "")
        logf("SKIP", ",".join(_disp_answer(topic, t, 31) for t in chosen), f" [{src}]")
    return _submit_next(ctx, cur_code, random.randint(8000, 12000))


def _verify_and_learn(ctx, topic, code, answer, mode, logf):
    """verify 一次并处理纠错入库。返回 (new_code, answer_result, corrects, over_status)。"""
    vr = _verify(ctx, code, answer)
    vd = vr.get("data") or {}
    new_code = vd.get("topic_code", code)
    ar = vd.get("answer_result")
    corrects = vd.get("answer_corrects") or []
    over = vd.get("over_status")
    if ar != 1:
        # 首次判定即错：即使后面用 corrects 纠正成功，这题的分也已经丢了
        ctx.stats["wrong"] += 1
        if corrects:
            _learn_from_corrects(ctx, topic, corrects)
    return new_code, ar, corrects, over


def _disp_answer(topic, answer, mode):
    opts = _opts(topic)
    if mode == 32 and isinstance(answer, str):
        return answer[:30]
    if isinstance(answer, int) and 0 <= answer < len(opts):
        return opts[answer].get("content", "?")[:30]
    return str(answer)[:30]


def setup_from_word_list(ctx, word_list_result: dict):
    """从选词列表响应构建 word_list + word_dict（easy handle_word_result 移植）。"""
    data = word_list_result.get("data") or {}
    if not isinstance(data, dict):
        return
    words = data.get("word_list") or []
    wl, wd = [], {}
    for item in words:
        if isinstance(item, dict):
            wl.append(item.get("word", ""))
            zh = item.get("word_zh")
            if zh:
                wd[zh] = item.get("word", "")
    wl = [w for w in wl if w]
    if wl:
        ctx.word_list = wl
    ctx.word_dict.update(wd)
