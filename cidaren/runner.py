# -*- coding: utf-8 -*-
"""
runner.py — 任务执行器（整合 LangQi99 的任务流程 + Cidaren-AI 的循环重刷闭环）

单轮：选词 -> StartAnswer -> 逐题 solve_topic -> 直到完成
多轮：满分即止；未满分 -> 重置选词 -> 重刷（最多 MAX_ROUNDS 轮）；连续两轮分数不涨则放弃
"""
from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutTimeout

from . import bank as bk
from .api import ApiError, SecurityVerifyError
from .config import as_int, get_runtime_config
from .solver import TopicContext, setup_from_word_list, solve_topic

MAX_PAGE = 20

# 单题执行线程池：超时后丢弃旧线程、换新线程，保证永不卡死
_executor = ThreadPoolExecutor(max_workers=1)
_executor_lock = threading.Lock()


def _refresh_executor():
    global _executor
    with _executor_lock:
        _executor.shutdown(wait=False)
        _executor = ThreadPoolExecutor(max_workers=1)


def _get_topic(resp):
    d = (resp or {}).get("data") or {}
    if isinstance(d, dict) and d.get("topic_code"):
        return d
    return d.get("topic_info") or d.get("topic") or (d.get("topic_list") or [None])[0]


def _score(client, source, task_id, release_id, course_id=None, list_id=None):
    """轻量查单个任务当前分数。"""
    try:
        if source == "study":
            resp = client.study_task_list(course_id=course_id or "CET4_v2")
            recs = (resp.get("data") or {}).get("task_list") or []
            for r in recs:
                if str(r.get("list_id")) == str(list_id or release_id) or str(r.get("task_id")) == str(task_id):
                    return r.get("score")
            return None
        page = 1
        while page <= MAX_PAGE:
            resp = client.page_task(page=page, size=50)
            recs = (resp.get("data") or {}).get("records") or []
            if not recs:
                return None
            for r in recs:
                if r.get("task_id") == task_id and r.get("release_id") == release_id:
                    return r.get("score")
            if len(recs) < 50:
                return None
            page += 1
    except (ApiError, SecurityVerifyError):
        return None
    return None


def _reset_task(ctx, words, max_score=10):
    """重置任务重新选词（Cidaren-AI 思路）。返回是否成功。"""
    todo = [w for w in words if w.get("score", 0) < max_score]
    if not todo:
        return False
    word_map = {}
    for w in todo:
        key = f"{w.get('course_id') or ctx.course_id}:{w.get('list_id') or ctx.list_id}"
        word_map.setdefault(key, []).append(w["word"])
    try:
        if ctx.is_study:
            resp = ctx.client.study_submit_chose_word(ctx.task_id, ctx.course_id, ctx.list_id, word_map,
                                                      task_type=ctx.task_type, grade=ctx.grade)
        else:
            resp = ctx.client.submit_chose_word(ctx.task_id, word_map, task_type=ctx.task_type)
        return resp.get("code") == 1
    except (ApiError, SecurityVerifyError):
        return False


def run_one_round(ctx: TopicContext, task_id, release_id, log=print):
    """执行一轮完整答题，返回该轮结束后的响应。"""
    ctx.task_id = task_id
    ctx.release_id = release_id
    ctx.bank = bk.bank_load()

    # 1. 选词（构建词表 + 提交待练词）
    try:
        if ctx.is_study:
            chose = ctx.client.study_chose_word_list(task_id, ctx.course_id, ctx.list_id,
                                                     task_type=ctx.task_type, grade=ctx.grade)
        else:
            chose = ctx.client.chose_word_list(task_id, task_type=ctx.task_type)
    except (ApiError, SecurityVerifyError) as e:
        log(f"  [选词失败] {e}")
        return None
    data = chose.get("data") or {}
    words = data.get("word_list") or []
    setup_from_word_list(ctx, chose)
    # 合并偷来的全局词表（easy word_pool.json，URL 编码 -> 明文），补 mode 32/73 反查
    try:
        from urllib.parse import unquote
        pool = bk.load_word_pool()
        for zh, en in pool.items():
            if zh and zh not in ctx.word_dict:
                ctx.word_dict[zh] = unquote(en)
        if pool:
            log(f"  [词表] 全局词表已挂载 {len(pool)} 条（偷来的）")
    except Exception:
        pass
    log(f"  [词表] 共 {len(words)} 词")
    todo = [w for w in words if w.get("score", 0) < 10]
    if todo:
        word_map = {}
        for w in todo:
            key = f"{w.get('course_id') or ctx.course_id}:{w.get('list_id') or ctx.list_id}"
            word_map.setdefault(key, []).append(w["word"])
        try:
            if ctx.is_study:
                ctx.client.study_submit_chose_word(task_id, ctx.course_id, ctx.list_id, word_map,
                                                   task_type=ctx.task_type, grade=ctx.grade)
            else:
                ctx.client.submit_chose_word(task_id, word_map, task_type=ctx.task_type)
            log(f"  [选词] 已勾选 {len(todo)} 个待练词")
        except (ApiError, SecurityVerifyError) as e:
            log(f"  [选词提交失败] {e}")
    else:
        log("  [选词] 全部满分，跳过勾选")

    # 2. 开始答题
    try:
        if ctx.is_study:
            resp = ctx.client.study_start_answer(task_id, ctx.course_id, ctx.list_id,
                                                 task_type=ctx.task_type, grade=ctx.grade)
        else:
            resp = ctx.client.start_answer(task_id, release_id, task_type=ctx.task_type)
    except (ApiError, SecurityVerifyError) as e:
        log(f"  [StartAnswer 失败] {e}")
        return None

    topic = _get_topic(resp)
    if not topic:
        log(f"  [StartAnswer 异常] {resp}")
        return None
    d = resp.get("data") or {}
    done, total = d.get("topic_done_num", 0), d.get("topic_total", 0)
    log(f"  [开始] {done}/{total}")

    # 3. 逐题循环（单题超时保护：线程池 + QUESTION_TIMEOUT 秒强制跳过）
    question_timeout = max(15, as_int(ctx.config.get("QUESTION_TIMEOUT"), 60))
    guard = 0
    while topic and topic.get("topic_code"):
        guard += 1
        if guard > 2000:
            log("  [保护] 超过 2000 题，强制结束本轮")
            break
        code = topic["topic_code"]
        qnum = done + 1
        log(f"  [题 {qnum}/{total or '?'}] 开始")
        next_resp = None
        future = _executor.submit(solve_topic, ctx, topic, log)
        try:
            next_resp = future.result(timeout=question_timeout)
        except _FutTimeout:
            log(f"  [题 {qnum}] 超时 {question_timeout}s 无响应，强制跳过该题")
            _refresh_executor()  # 丢弃卡死的线程，换新线程继续
            try:
                if ctx.is_study:
                    next_resp = ctx.client.study_submit(code, 20000)
                else:
                    next_resp = ctx.client.submit(code, 20000)
            except (ApiError, SecurityVerifyError) as e:
                log(f"  [题 {qnum}] 跳过提交失败: {e}，尝试继续")
                continue
        except SecurityVerifyError as e:
            log(f"  [风控] {e}，本轮终止")
            return None
        except ApiError as e:
            log(f"  [网络异常] {e}，重试下一题")
            time.sleep(3)
            continue

        if next_resp is None:
            break
        nt = _get_topic(next_resp)
        sd = next_resp.get("data") or {}
        done = sd.get("topic_done_num", done + 1)
        total = sd.get("topic_total", total)
        if not nt or not nt.get("topic_code") or nt.get("topic_code") == code:
            log(f"  [完成] {done}/{total}")
            break
        topic = nt
        # 节奏控制（Cidaren-AI 思路，DELAY_PER_QUESTION=0 时测试提速）
        gap = max(0.0, as_int(ctx.config.get("DELAY_PER_QUESTION"), 2))
        if gap <= 0:
            time.sleep(random.uniform(0.02, 0.15))
        elif topic.get("topic_mode") == 0:
            time.sleep(random.uniform(0.3, 0.8))
        else:
            time.sleep(random.uniform(gap * 0.8, gap * 1.3))

    # 4. 签到
    try:
        sr = ctx.client.signin()
        sd = sr.get("data") or {}
        if sd:
            log(f"  [签到] 累计 {sd.get('sign_in_total')} 天，积分 +{sd.get('integral')}")
    except (ApiError, SecurityVerifyError):
        pass
    return resp


def run_full(client, task_id, release_id, config=None, log=print, task_type=1):
    """班级任务：循环重刷到满分。"""
    config = config or get_runtime_config()
    max_rounds = max(1, as_int(config.get("MAX_ROUNDS"), 10))
    ctx = TopicContext(client, config, task_kind="class", task_type=task_type, release_id=release_id)

    prev_score = _score(client, "class", task_id, release_id) or 0
    stuck = 0
    for round_num in range(1, max_rounds + 1):
        if round_num > 1:
            log(f"  --- 第 {round_num} 轮 ---")
            time.sleep(1)
        run_one_round(ctx, task_id, release_id, log=log)
        score = _score(client, "class", task_id, release_id)
        log(f"  [分数] {score if score is not None else '?'}")
        if score is not None and score >= 100:
            log(f"  [满分] 任务完成")
            return
        if score is None:
            return
        if score <= prev_score + 0.1:
            stuck += 1
        else:
            stuck = 0
        prev_score = score
        if stuck >= 2:
            log(f"  [卡住] 连续 {stuck} 轮分数未提升 ({score}分)，放弃该任务")
            return
        if round_num < max_rounds:
            # 重置重刷
            try:
                if ctx.is_study:
                    chose = ctx.client.study_chose_word_list(task_id, ctx.course_id, ctx.list_id,
                                                             task_type=ctx.task_type, grade=ctx.grade)
                else:
                    chose = ctx.client.chose_word_list(task_id, task_type=ctx.task_type)
                words = (chose.get("data") or {}).get("word_list") or []
            except (ApiError, SecurityVerifyError):
                words = []
            if _reset_task(ctx, words):
                log("  [重置] 成功，开始新一轮")
                continue
            log("  [重置] 失败，停止")
            return
    log(f"  [轮次用尽] 已达最大 {max_rounds} 轮")


def run_study_full(client, task_id, course_id, list_id, task_type=3, grade=2, config=None, log=print):
    """自学任务：循环重刷到满分。"""
    config = config or get_runtime_config()
    max_rounds = max(1, as_int(config.get("MAX_ROUNDS"), 10))
    try:
        task_type = int(task_type)
        grade = int(grade)
    except (TypeError, ValueError):
        task_type, grade = 3, 2
    ctx = TopicContext(client, config, task_kind="study", course_id=course_id, list_id=list_id,
                       task_type=task_type, grade=grade)

    prev_score = _score(client, "study", task_id, list_id, course_id, list_id) or 0
    stuck = 0
    for round_num in range(1, max_rounds + 1):
        if round_num > 1:
            log(f"  --- 第 {round_num} 轮 ---")
            time.sleep(1)
        run_one_round(ctx, task_id, list_id, log=log)
        score = _score(client, "study", task_id, list_id, course_id, list_id)
        log(f"  [分数] {score if score is not None else '?'}")
        if score is not None and score >= 100:
            log(f"  [满分] 任务完成")
            return
        if score is None:
            return
        if score <= prev_score + 0.1:
            stuck += 1
        else:
            stuck = 0
        prev_score = score
        if stuck >= 2:
            log(f"  [卡住] 连续 {stuck} 轮分数未提升 ({score}分)，放弃该任务")
            return
        if round_num < max_rounds:
            try:
                chose = ctx.client.study_chose_word_list(task_id, course_id, list_id,
                                                         task_type=task_type, grade=grade)
                words = (chose.get("data") or {}).get("word_list") or []
            except (ApiError, SecurityVerifyError):
                words = []
            if _reset_task(ctx, words):
                log("  [重置] 成功，开始新一轮")
                continue
            log("  [重置] 失败，停止")
            return
    log(f"  [轮次用尽] 已达最大 {max_rounds} 轮")
