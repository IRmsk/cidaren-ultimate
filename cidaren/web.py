# -*- coding: utf-8 -*-
"""
web.py — Web 控制台（基于 LangQi99/cidaren 增强，含 chuyc11/xuhaoranbb 的断点恢复与测试报告）

页面能力：
1. 配置中心：鉴权（Token / 扫码登录）/ 自学 / LLM / AI 策略 / 答题节奏 / OCR，
   表单实时读写 .env（保存即生效）
2. 任务面板：班级 + 自学任务，启动 / 循环刷满 / 停止 / 断点恢复 / 实时日志
3. 测试报告：每题判分与来源统计（cache / rule / ai / blank / skip）

架构说明（重要）：
- 刷题由「子进程」执行（python -m cidaren class/study ...），本服务只负责调度、
  状态跟踪与日志转发。好处：页面刷新/关掉浏览器都不打断刷题，也方便循环重刷。
- JOBS 表以 (source, task_id, release_id) 为键，保存每个任务的子进程句柄、
  日志环形缓冲（上限 LOG_MAX 行）与循环轮次。
- _reader_thread 后台线程持续读子进程 stdout 写进日志缓冲；任务结束后，
  若开了循环且未满分则自动间隔 5s 重启下一轮（断点恢复的核心）。
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import threading
import time
from collections import deque

from flask import Flask, jsonify, request, Response

from . import bank as bk
from . import report as rp
from . import ai as ai_mod
from .config import (
    build_subprocess_env,
    env_file_path,
    get_missing_auth_fields,
    get_runtime_config,
    save_runtime_config,
)

app = Flask(__name__)

# ------------------------------------------------------------------
# 任务调度状态
#   JOBS:   { (source, task_id, release_id): job }  任务索引
#   job 字段: proc=子进程句柄, logs=日志环形缓冲, done=是否结束,
#            loop=是否循环刷, round=当前轮次, stopped=用户是否手动停止
# ------------------------------------------------------------------
JOBS = {}
JOBS_LOCK = threading.Lock()
LOG_MAX = 800  # 每个任务最多保留的日志行数（环形缓冲，防内存膨胀）

# ------------------------------------------------------------------
# 扫码登录会话
#   token:   后端生成的授权 user_token（未确认前不写入配置）
#   started: 二维码生成时间戳，用于 10 分钟过期判断
# ------------------------------------------------------------------
QR_STATE = {"token": None, "started": 0.0}
QR_LOCK = threading.Lock()


def _job_key(source, task_id, release_id):
    """生成任务唯一键：(来源, 任务ID, 发布ID) 三元组。"""
    return ((source or "class"), str(task_id), str(release_id))


def _int_or_default(value, default):
    """安全转 int；失败返回默认值（配置项可能是空串/None）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_job(source, task_id, release_id):
    """按键查任务；自学任务(list_id)与 release_id 是同一含义，做了兼容查找。"""
    job = JOBS.get(_job_key(source, task_id, release_id))
    if job or source != "study":
        return job
    for (job_source, _tid, job_rid), candidate in JOBS.items():
        if job_source == "study" and job_rid == str(release_id):
            return candidate
    return None


def _client(config=None):
    """构造词达人 API 客户端；缺鉴权字段时抛出带提示的 ValueError。"""
    from .api import Client
    cfg = config or get_runtime_config()
    missing = get_missing_auth_fields(cfg)
    if missing:
        raise ValueError(f"请先在配置面板填写: {', '.join(missing)}")
    return Client(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))


def _list_class_tasks(c):
    """分页拉取全部班级任务（每页 50，最多 20 页）。"""
    out, page = [], 1
    while True:
        resp = c.page_task(page=page, size=50)
        recs = (resp.get("data") or {}).get("records") or []
        if not recs:
            break
        out.extend(recs)
        if len(recs) < 50:
            break
        page += 1
        if page > 20:
            break
    return out


def _list_study_tasks(c, course_id):
    """拉取指定课程下的全部自学任务。"""
    resp = c.study_task_list(course_id=course_id)
    data = resp.get("data") or {}
    return data.get("task_list") or []


def _list_tasks():
    """
    汇总班级 + 自学两类任务，并附带每个任务当前的运行状态
    （running / done / loop / round），供前端表格直接渲染。
    某类接口失败不影响另一类，失败信息进 warnings 由前端展示。
    """
    cfg = get_runtime_config()
    c = _client(cfg)
    course_id = (cfg.get("COURSE_ID") or "CET4_v2").strip() or "CET4_v2"
    study_grade = _int_or_default(cfg.get("STUDY_GRADE"), 2)
    out, warnings = [], []
    try:
        for r in _list_class_tasks(c):
            tid, rid = r.get("task_id"), r.get("release_id")
            job = _find_job("class", tid, rid)
            out.append({
                "source": "class", "source_label": "班级", "can_start": True,
                "task_id": tid, "release_id": rid, "task_name": r.get("task_name"),
                "progress": r.get("progress"), "score": r.get("score"),
                "running": bool(job and not job["done"]), "done": bool(job and job["done"]),
                "exit_code": job["exit_code"] if job else None,
                "loop": bool(job and job.get("loop")), "round": job.get("round", 0) if job else 0,
                "task_type": r.get("task_type"),
            })
    except Exception as e:
        warnings.append(f"班级任务读取失败: {e}")
    try:
        for r in _list_study_tasks(c, course_id):
            tid, rid = r.get("task_id"), r.get("list_id")
            job = _find_job("study", tid, rid)
            out.append({
                "source": "study", "source_label": "自学", "can_start": True,
                "task_id": tid, "release_id": rid, "course_id": r.get("course_id") or course_id,
                "list_id": rid, "task_type": r.get("task_type"),
                "grade": _int_or_default(r.get("grade"), study_grade),
                "task_name": r.get("task_name"), "progress": r.get("progress"), "score": r.get("score"),
                "running": bool(job and not job["done"]), "done": bool(job and job["done"]),
                "exit_code": job["exit_code"] if job else None,
                "loop": bool(job and job.get("loop")), "round": job.get("round", 0) if job else 0,
            })
    except Exception as e:
        warnings.append(f"自学任务读取失败: {e}")
    return out, warnings


def _reader_thread(job_id, proc):
    """
    日志读取后台线程：
    1. 逐行读子进程 stdout 写进任务的日志环形缓冲
    2. 进程退出后标记 done / exit_code
    3. 循环模式：查最新分数，未满分则 5s 后自动重启下一轮（断点恢复）
    """
    job = JOBS[job_id]
    for line in iter(proc.stdout.readline, b""):
        try:
            text = line.decode("utf-8", errors="replace").rstrip()
        except Exception:
            text = repr(line)
        job["logs"].append(text)
    proc.wait()
    job["done"] = True
    job["exit_code"] = proc.returncode
    if job.get("loop") and not job.get("stopped"):
        try:
            score = _query_score(job["source"], job["task_id"], job["release_id"],
                                 job.get("course_id"), job.get("list_id"))
        except Exception:
            score = None
        if score is not None and score >= 100:
            job["logs"].append(f"[loop] 已满分 ({score}), 停止循环")
            return
        job["logs"].append(f"[loop] 当前分数={score}, 5s 后重新启动...")
        time.sleep(5)
        if job.get("stopped"):
            return
        with JOBS_LOCK:
            _spawn_job(job["source"], job["task_id"], job["release_id"], loop=True,
                       course_id=job.get("course_id"), list_id=job.get("list_id"),
                       task_type=job.get("task_type"), grade=job.get("grade"))


def _query_score(source, task_id, release_id, course_id=None, list_id=None):
    """查询任务当前分数（自学走 study_task_list，班级走 page_task 匹配）。"""
    c = _client()
    if source == "study":
        resp = c.study_task_list(course_id=course_id or "CET4_v2")
        recs = (resp.get("data") or {}).get("task_list") or []
        for r in recs:
            if str(r.get("list_id")) == str(list_id or release_id) or str(r.get("task_id")) == str(task_id):
                return r.get("score")
        return None
    page = 1
    while page <= 20:
        resp = c.page_task(page=page, size=50)
        recs = (resp.get("data") or {}).get("records") or []
        if not recs:
            return None
        for r in recs:
            if r.get("task_id") == task_id and r.get("release_id") == release_id:
                return r.get("score")
        if len(recs) < 50:
            return None
        page += 1
    return None


def _spawn_job(source, task_id, release_id, loop=False, config=None, course_id=None,
               list_id=None, task_type=None, grade=None):
    """
    启动一个刷题子进程并登记到 JOBS：
    - 班级: python -m cidaren class <task_id> <release_id> <task_type>
    - 自学: python -m cidaren study <task_id> <list_id> <course_id> <task_type> <grade>
    - 环境变量由 build_subprocess_env 注入（USERTOKEN/LLM 等配置）
    - 同任务已跑过一轮时保留旧日志，追加"第 N 轮启动"分隔线
    """
    if source == "study":
        args = ["study", str(task_id), str(list_id or release_id), str(course_id or "CET4_v2"),
                str(task_type or 3), str(grade or 2)]
    else:
        args = ["class", str(task_id), str(release_id), str(task_type or 1)]
    proc = subprocess.Popen(
        [sys.executable, "-u", "-m", "cidaren", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, env=build_subprocess_env(config),
    )
    key = _job_key(source, task_id, release_id)
    old = JOBS.get(key)
    logs = old["logs"] if old else deque(maxlen=LOG_MAX)
    if old:
        logs.append(f"========== 第 {old.get('round', 1) + 1} 轮启动 ==========")
    JOBS[key] = {
        "proc": proc, "logs": logs, "started": time.time(), "done": False,
        "exit_code": None, "source": source, "release_id": release_id, "task_id": task_id,
        "course_id": course_id, "list_id": list_id, "task_type": task_type, "grade": grade,
        "loop": loop, "stopped": False, "round": (old.get("round", 1) + 1) if old else 1,
    }
    threading.Thread(target=_reader_thread, args=(key, proc), daemon=True).start()


# ==================================================================
# API 路由（前端 fetch 全部指向这里，返回 JSON）
#   页面    GET  /                → 控制台 HTML
#   配置    GET/POST /api/config  → 读取/保存 .env
#   任务    GET  /api/tasks       → 班级+自学任务列表（含运行状态）
#   登录    POST /api/login_qr · GET /api/login_poll → 扫码登录
#   操作    POST /api/start · /api/start_all · /api/stop → 启动/断点恢复/停止
#   日志    GET  /api/logs        → 实时日志轮询
#   报告    GET  /api/reports · /api/report → 测试报告统计与明细
# ==================================================================

@app.route("/")
def index():
    """渲染控制台页面（内嵌 HTML/CSS/JS，单文件无外部依赖）。"""
    return Response(_INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/api/config")
def api_config():
    """读取当前 .env 配置 + 鉴权缺失字段 + 答案库统计，供表单回填。"""
    config = get_runtime_config()
    return jsonify({
        "ok": True, "config": config, "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(config),
        "stats": bk.lib_stats(),
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    """保存前端提交的整份配置到 .env（白名单由 save_runtime_config 把关）。"""
    body = request.get_json(force=True) or {}
    saved = save_runtime_config(body)
    return jsonify({
        "ok": True, "config": saved, "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(saved),
        "stats": bk.lib_stats(),
    })


@app.route("/api/test_llm", methods=["POST"])
def api_test_llm():
    """测试 AI 接口可用性：用表单当前值（未保存也能测）发一个最小请求。

    参数缺省回退到 .env 已保存配置；返回 {"ok", "latency_ms", "reply", "error"}。
    """
    body = request.get_json(force=True) or {}
    cfg = get_runtime_config()
    llm_url = (body.get("llm_url") or cfg.get("LLM_URL") or "").strip()
    llm_key = (body.get("llm_key") or cfg.get("LLM_KEY") or "").strip()
    llm_model = (body.get("llm_model") or cfg.get("LLM_MODEL") or "").strip()
    max_tokens = body.get("llm_max_tokens") or cfg.get("LLM_MAX_TOKENS") or 32
    result = ai_mod.test_llm_connectivity(llm_url, llm_key, llm_model, max_tokens)
    return jsonify(result)


@app.route("/api/tasks")
def api_tasks():
    """返回任务列表；缺鉴权字段时 400，接口异常时 500，正常附带统计。"""
    try:
        tasks, warnings = _list_tasks()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tasks": tasks, "warnings": warnings, "stats": bk.lib_stats()})


@app.route("/api/token_check")
def api_token_check():
    """校验 USERTOKEN 有效性，返回学生姓名或错误信息。"""
    from .login import is_token_valid
    cfg = get_runtime_config()
    missing = get_missing_auth_fields(cfg)
    if missing:
        return jsonify({"ok": False, "error": f"缺少 USERTOKEN"}), 400
    ok, info = is_token_valid(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))
    if ok:
        name = (info or {}).get("student_name") or (info or {}).get("nick_name") or "?"
        return jsonify({"ok": True, "name": name})
    return jsonify({"ok": False, "error": "Token 无效或已过期"})


@app.route("/api/login_qr", methods=["POST"])
def api_login_qr():
    """生成扫码登录二维码。返回 user_token / base64 图片 / 授权链接。"""
    from .login import qr_authorize
    try:
        with QR_LOCK:
            session = qr_authorize()
            QR_STATE["token"] = session["user_token"]
            QR_STATE["started"] = time.time()
        return jsonify({
            "ok": True,
            "user_token": session["user_token"],
            "qr_base64": session.get("qr_base64"),
            "redirect_url": session.get("redirect_url"),
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/login_poll")
def api_login_poll():
    """轮询扫码结果。成功后把 USERTOKEN 写进配置并返回学生姓名。"""
    from .login import is_token_valid
    with QR_LOCK:
        token = QR_STATE.get("token")
        started = QR_STATE.get("started", 0)
    if not token:
        return jsonify({"ok": False, "error": "尚未生成二维码"})
    if time.time() - started > 600:
        return jsonify({"ok": False, "error": "二维码已过期，请重新生成"})
    ok, info = is_token_valid(token)
    if not ok:
        return jsonify({"ok": False, "pending": True})
    cfg = get_runtime_config()
    cfg["USERTOKEN"] = token
    save_runtime_config(cfg)
    name = (info or {}).get("student_name") or (info or {}).get("nick_name") or "?"
    return jsonify({"ok": True, "name": name})


@app.route("/api/start", methods=["POST"])
def api_start():
    """
    启动单个任务（前端"启动/循环"按钮）。
    同一任务已在运行时不重复启动；loop=true 表示刷到满分自动重刷。
    """
    body = request.get_json(force=True)
    source = body.get("source") or "class"
    task_id = int(body["task_id"])
    release_id = body["release_id"]
    if source == "class":
        release_id = int(release_id)
    course_id = body.get("course_id")
    list_id = body.get("list_id") or release_id
    task_type = int(body.get("task_type") or 3)
    config = get_runtime_config()
    grade = _int_or_default(body.get("grade") or config.get("STUDY_GRADE"), 2)
    loop = bool(body.get("loop", False))
    missing = get_missing_auth_fields(config)
    if missing:
        return jsonify({"ok": False, "error": f"请先填写配置: {', '.join(missing)}"}), 400
    with JOBS_LOCK:
        job = _find_job(source, task_id, release_id)
        if job and not job["done"]:
            return jsonify({"ok": False, "error": "任务正在运行"}), 400
        _spawn_job(source, task_id, release_id, loop=loop, config=config,
                   course_id=course_id, list_id=list_id, task_type=task_type, grade=grade)
    return jsonify({"ok": True})


@app.route("/api/start_all", methods=["POST"])
def api_start_all():
    """
    断点恢复：把全部「未满分且未运行」的班级任务以循环模式启动。
    返回 started（新启动数）/ skipped（跳过数：运行中或已满分）。
    """
    try:
        config = get_runtime_config()
        missing = get_missing_auth_fields(config)
        if missing:
            return jsonify({"ok": False, "error": f"请先填写配置: {', '.join(missing)}"}), 400
        c = _client(config)
        tasks = _list_class_tasks(c)
        started = skipped = 0
        with JOBS_LOCK:
            for r in tasks:
                tid, rid = r.get("task_id"), r.get("release_id")
                job = _find_job("class", tid, rid)
                if job and not job["done"]:
                    skipped += 1
                    continue
                if r.get("score") is not None and r.get("score") >= 100:
                    skipped += 1
                    continue
                _spawn_job("class", tid, rid, loop=True, config=config,
                           task_type=r.get("task_type") or 1)
                started += 1
        return jsonify({"ok": True, "started": started, "skipped": skipped})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/stop", methods=["POST"])
def api_stop():
    """停止任务：标记 stopped/取消循环，并 terminate 子进程。"""
    body = request.get_json(force=True)
    source = body.get("source") or "class"
    task_id = int(body["task_id"])
    release_id = body["release_id"]
    if source == "class":
        release_id = int(release_id)
    job = _find_job(source, task_id, release_id)
    if not job:
        return jsonify({"ok": False, "error": "任务未运行"}), 400
    job["stopped"] = True
    job["loop"] = False
    if not job["done"]:
        try:
            job["proc"].terminate()
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/api/reports")
def api_reports():
    """返回全部测试报告的任务级统计（用于报告列表卡片）。"""
    return jsonify({"ok": True, "reports": rp.list_reports()})


@app.route("/api/report")
def api_report_detail():
    """返回单个任务的报告统计 + 最近 200 条判分明细。"""
    key = request.args.get("key", "")
    if not key:
        return jsonify({"ok": False, "error": "缺少 key"}), 400
    stats = rp.report_stats(key)
    recs = rp.report_load(key)[-200:]
    return jsonify({"ok": True, "stats": stats, "recs": recs})


@app.route("/api/logs")
def api_logs_query():
    """实时日志轮询：返回任务日志缓冲 + 是否结束 + 退出码。"""
    source = request.args.get("source") or "class"
    task_id = request.args.get("task_id")
    release_id = request.args.get("release_id")
    job = _find_job(source, task_id, release_id)
    if not job:
        return jsonify({"ok": False, "logs": []})
    return jsonify({"ok": True, "logs": list(job["logs"]), "done": job["done"],
                    "exit_code": job["exit_code"]})


# ================= HTML =================

_INDEX_HTML = r'''<!doctype html>
<html lang="zh">
<head>
  <meta charset="utf-8" />
  <!-- viewport: 移动端适配的关键，禁止缩放以保布局稳定 -->
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <meta name="theme-color" content="#f5f7fb" />
  <title>cidaren-ultimate 控制台</title>
  <style>
    /* ============================================================
       1. 全局变量与基础样式
       —— 用 CSS 变量统一色板，后面所有组件只引用变量，改主题只需改这里
       ============================================================ */
    :root {
      --bg: #f5f7fb;            /* 页面背景 */
      --card: #ffffff;          /* 卡片背景 */
      --border: #eaecf0;        /* 卡片描边 */
      --text: #1d1d1f;          /* 主文字 */
      --muted: #667085;         /* 次要文字 */
      --primary: #007aff;       /* 主操作色（启动/保存） */
      --primary-dark: #0062cc;  /* 主操作色 hover */
      --loop: #5856d6;          /* 循环/断点恢复紫色 */
      --loop-dark: #4845b0;
      --danger: #ff3b30;        /* 停止/危险操作 */
      --ok: #34c759;            /* 成功/满分 */
      --warn-bg: #fff7ed;
      --warn-text: #b54708;
      --radius: 14px;
    }
    * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
    html, body { margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--text);
      /* 手机端 padding 更小，底部留出 iPhone 安全区 */
      padding: 24px;
      padding-bottom: calc(24px + env(safe-area-inset-bottom));
    }
    h1 { font-size: 26px; margin: 0 0 8px; }
    .subtitle { margin: 0 0 20px; color: var(--muted); font-size: 14px; line-height: 1.6; }

    /* ============================================================
       2. 卡片容器
       ============================================================ */
    .layout { display: flex; flex-direction: column; gap: 20px; }
    .card {
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: 0 8px 24px rgba(15,23,42,.04);
      overflow: hidden;
    }
    /* 卡片头部：标题在左，状态/按钮在右；窄屏自动换行 */
    .card-head {
      padding: 18px 20px;
      border-bottom: 1px solid #f2f4f7;
      display: flex; justify-content: space-between; align-items: center;
      gap: 12px; flex-wrap: wrap;
    }
    .card-head h2 { margin: 0; font-size: 18px; }
    .card-head p { margin: 4px 0 0; color: var(--muted); font-size: 13px; line-height: 1.5; }
    .card-body { padding: 20px; }

    /* ============================================================
       3. 按钮
       —— 手机端统一放大触控目标（min-height 44px，Apple 推荐）
       ============================================================ */
    button {
      font: inherit;
      padding: 8px 16px;
      min-height: 40px;
      border-radius: 10px;
      border: 1px solid #d2d2d7;
      background: #fff;
      cursor: pointer;
      transition: background .15s;
    }
    button:hover { background: #f0f0f0; }
    button:active { transform: scale(.97); }
    button:disabled { opacity: .5; cursor: not-allowed; }
    button.primary { background: var(--primary); color: #fff; border-color: var(--primary); }
    button.primary:hover { background: var(--primary-dark); }
    button.danger { background: var(--danger); color: #fff; border-color: var(--danger); }
    button.loop { background: var(--loop); color: #fff; border-color: var(--loop); }
    button.loop:hover { background: var(--loop-dark); }
    button.green { background: var(--ok); color: #fff; border-color: var(--ok); }
    .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }

    /* ============================================================
       4. 配置表单
       —— 双列栅格；窄屏自动变单列
       ============================================================ */
    .grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(280px, 1fr));
      gap: 16px;
    }
    .full { grid-column: 1 / -1; }  /* 跨整行的字段 */
    .field { display: flex; flex-direction: column; gap: 6px; }
    .field label { font-size: 13px; font-weight: 600; color: #344054; }
    .field input {
      width: 100%;
      border: 1px solid #d0d5dd;
      border-radius: 10px;
      padding: 10px 12px;
      font: inherit;
      font-size: 16px;             /* ≥16px 防止 iOS 聚焦自动放大 */
    }
    .field input:focus { outline: 2px solid rgba(0,122,255,.25); border-color: var(--primary); }
    .field small { color: var(--muted); font-size: 12px; line-height: 1.5; }

    /* LLM 连接测试结果：ok 绿 / fail 红，小字就地显示 */
    .llm-test-result { font-size: 12px; line-height: 1.5; color: var(--muted); }
    .llm-test-result.ok { color: #027a48; }
    .llm-test-result.fail { color: #d92d20; }

    /* ============================================================
       5. 状态徽章 / 统计胶囊
       ============================================================ */
    .bar {
      display: flex; gap: 12px; align-items: center;
      margin-bottom: 16px; font-size: 13px; color: #666;
      flex-wrap: wrap;
    }
    .status-box {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 8px 12px; border-radius: 999px;
      background: #f8fafc; border: 1px solid #e2e8f0;
      color: #475467; font-size: 12px;
      max-width: 100%;
      word-break: break-all;
    }
    .status-box.ok { background: #ecfdf3; color: #027a48; border-color: #d1fadf; }
    .status-box.warn { background: var(--warn-bg); color: var(--warn-text); border-color: #fed7aa; }
    .stat-pill {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 6px 12px; border-radius: 999px;
      background: #eef2ff; color: #4338ca; font-size: 12px;
    }
    .inline-code { font-family: ui-monospace, Menlo, monospace; background: #f2f4f7; padding: 2px 6px; border-radius: 6px; }

    /* ============================================================
       6. 表格（任务 / 报告）
       —— 每列用 data-label 标注列名，窄屏时转成卡片式列表（见响应式段）
       ============================================================ */
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
    th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
    th { background: #fafafa; font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; }
    tr:last-child td { border-bottom: none; }
    .progress-bar {
      width: 100px; height: 6px; background: #e8e8ed; border-radius: 3px;
      overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px;
    }
    .progress-bar > div { height: 100%; background: var(--ok); transition: width .3s; }
    .score { font-weight: 600; }
    .score.full { color: var(--ok); }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
    .badge.run { background: #fff3cd; color: #856404; }
    .badge.done { background: #d4edda; color: #155724; }

    /* ============================================================
       7. 弹窗（日志 / 扫码）
       ============================================================ */
    .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 100; }
    .modal.show { display: flex; align-items: center; justify-content: center; }
    .modal-body {
      background: #1e1e1e; color: #d4d4d4;
      width: 80vw; max-width: 760px; height: 75vh;
      border-radius: 12px; padding: 16px;
      display: flex; flex-direction: column;
    }
    .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; color: #fff; gap: 12px; }
    .modal-logs {
      flex: 1; overflow-y: auto;
      font-family: ui-monospace, Menlo, "SF Mono", monospace;
      font-size: 12px; line-height: 1.5;
    }

    /* ============================================================
       日志行（美化版）：徽章 + 内容 + 来源，状态色区分
       ------------------------------------------------------------
       行结构： [题型] [状态徽章] 内容 …… [来源徽章]
       颜色语义： 对=绿 / 学(纠错入库)=橙 / 答(进行中)=琥珀 /
                  AI=蓝 / 规则=灰蓝 / 纠错=紫 / 失败=红 / 信息=蓝 / 成功=绿
       ============================================================ */
    .log-line {
      display: flex; align-items: baseline; gap: 6px;
      padding: 4px 8px; border-bottom: 1px solid rgba(255,255,255,.05);
      word-break: break-word;
    }
    .log-line:hover { background: rgba(255,255,255,.04); }
    .lg-mode {          /* 题型编号 [15] */
      flex: none; font-size: 11px; color: #8e8e93;
      background: rgba(255,255,255,.08); border-radius: 4px; padding: 1px 5px;
    }
    .lg-badge {         /* 状态徽章：对 / 学 / 答 */
      flex: none; font-size: 11px; font-weight: 600;
      border-radius: 4px; padding: 1px 6px; white-space: nowrap;
    }
    .lg-badge.ok  { color: #34c759; background: rgba(52,199,89,.16); }
    .lg-badge.fix { color: #ff9f0a; background: rgba(255,159,10,.16); }
    .lg-badge.ask { color: #ffd60a; background: rgba(255,214,10,.14); }
    .lg-badge.err { color: #ff453a; background: rgba(255,69,58,.16); }
    .lg-msg {           /* 内容：占满剩余宽度 */
      flex: 1 1 auto; color: #d4d4d4; white-space: normal;
    }
    .lg-msg.muted { color: #8e8e93; }
    .lg-msg.err { color: #ff453a; }
    .lg-src {           /* 来源徽章 [rule]/[llm]/[corrects] */
      flex: none; font-size: 11px; border-radius: 4px; padding: 1px 6px; white-space: nowrap;
      color: #a9b7d4; background: rgba(138,155,180,.14);
    }
    .lg-src.rule     { color: #64d2ff; background: rgba(100,210,255,.14); }
    .lg-src.llm      { color: #0a84ff; background: rgba(10,132,255,.16); }
    .lg-src.corrects { color: #bf5af2; background: rgba(191,90,242,.16); }
    .lg-src.cache    { color: #30d158; background: rgba(48,209,88,.14); }
    .lg-src.blank    { color: #ff9f0a; background: rgba(255,159,10,.14); }
    .lg-tag {           /* 区块头 [词表]/[选词]/[题 x/y]/[开始] 等 */
      flex: none; font-size: 11px; font-weight: 600; border-radius: 4px; padding: 1px 6px;
      white-space: nowrap; color: #0a84ff; background: rgba(10,132,255,.14);
    }
    .lg-tag.ok  { color: #34c759; background: rgba(52,199,89,.16); }
    .lg-tag.err { color: #ff453a; background: rgba(255,69,58,.16); }
    .lg-tag.info{ color: #8e8e93; background: rgba(255,255,255,.08); }
    /* 扫码弹窗：白底小窗，宽度自适应 */
    .qr-box { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 24px; }
    .qr-box img { width: 240px; height: 240px; border-radius: 8px; }
    .qr-modal { width: min(88vw, 360px); height: auto; background: #fff; color: #1d1d1f; }

    /* ============================================================
       8. 响应式（移动端适配核心）
       —— 断点 900px：配置栅格变单列
       —— 断点 768px：表格转卡片、按钮加大、日志全屏、安全区
       ============================================================ */
    @media (max-width: 900px) {
      body { padding: 16px; }
      .grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 768px) {
      body { padding: 12px; padding-bottom: calc(12px + env(safe-area-inset-bottom)); }
      h1 { font-size: 22px; }
      .subtitle { font-size: 13px; }
      .card-head { padding: 14px 16px; }
      .card-body { padding: 16px; }

      /* 表格 → 卡片列表：隐藏表头，每行成一张卡，每格成"列名: 值"一行 */
      table, tbody, tr, td { display: block; width: 100%; }
      thead { display: none; }
      tr {
        border: 1px solid var(--border);
        border-radius: 12px;
        margin-bottom: 12px;
        overflow: hidden;
        box-shadow: 0 2px 6px rgba(15,23,42,.04);
      }
      td {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 12px;
        padding: 10px 14px;
        border-bottom: 1px dashed #f0f0f0;
      }
      /* data-label 在窄屏充当列名（宽屏时隐藏） */
      td::before { content: attr(data-label); color: var(--muted); font-size: 12px; flex-shrink: 0; }
      td:last-child { border-bottom: none; }
      /* 单元格值容器：占满剩余宽度、右对齐、允许换行，防长任务名截断 */
      td .cell-val { flex: 1; min-width: 0; text-align: right; word-break: break-all; }
      td .cell-val .progress-bar { width: 120px; }
      td .cell-val .btn-wrap { display: flex; gap: 8px; flex-wrap: wrap; justify-content: flex-end; }

      /* 触控按钮放大，操作列按钮换行排列 */
      button { padding: 10px 16px; min-height: 44px; }
      .bar { gap: 8px; }

      /* 日志弹窗全屏（看长日志体验更好） */
      .modal-body { width: 100vw; max-width: none; height: 100dvh; border-radius: 0; padding: 12px; }
      .modal-body.qr-modal { height: auto; border-radius: 12px; }
    }
  </style>
</head>
<body>

  <!-- ==================== 页头 ==================== -->
  <h1>cidaren-ultimate</h1>
  <p class="subtitle">词达人全自动刷题整合版：班级 + 自学任务 · 全题型规则引擎 · AI 优先答题 · 答案库自学习 · 扫码登录</p>

  <div class="layout">

    <!-- ==================== 卡片 1：配置中心 ==================== -->
    <section class="card">
      <div class="card-head">
        <div><h2>配置中心</h2><p>token / LLM / AI 策略，保存后写入 <span class="inline-code">.env</span>。</p></div>
        <!-- 鉴权状态胶囊：配置完整显示绿色，缺字段显示橙色警告 -->
        <div id="config-pill" class="status-box">读取中...</div>
      </div>
      <div class="card-body">
        <!-- 快捷操作栏 -->
        <div class="bar">
          <button class="green" onclick="openQr()">微信扫码登录</button>
          <button onclick="checkToken()">校验 Token</button>
          <div id="stats-box" class="status-box">答案库: -</div>
        </div>
        <!-- 配置表单：两列栅格，每个字段带说明 -->
        <div class="grid">
          <div class="field">
            <label for="USERTOKEN">USERTOKEN</label>
            <input id="USERTOKEN" type="password" autocomplete="off" />
            <small>词达人请求头中的 usertoken（Fiddler 抓包或扫码登录获取）。</small>
          </div>
          <div class="field">
            <label for="ABC">ABC</label>
            <input id="ABC" type="password" autocomplete="off" />
            <small>词达人请求头中的 abc（可选）。</small>
          </div>
          <div class="field full">
            <label for="AUTH_V">AUTH_V</label>
            <input id="AUTH_V" type="password" autocomplete="off" />
            <small>词达人请求头中的 authorization-v（可选）。</small>
          </div>
          <div class="field">
            <label for="COURSE_ID">COURSE_ID</label>
            <input id="COURSE_ID" type="text" placeholder="CET4_v2" />
            <small>自学任务课程 ID。</small>
          </div>
          <div class="field">
            <label for="STUDY_GRADE">STUDY_GRADE</label>
            <input id="STUDY_GRADE" type="number" min="1" max="4" placeholder="2" />
            <small>自学模式: 1 快速 / 2 普通 / 3 完整 / 4 超级困难。</small>
          </div>
          <div class="field">
            <label for="LLM_URL">LLM_URL</label>
            <input id="LLM_URL" type="text" placeholder="https://api.deepseek.com" />
            <small>OpenAI 兼容接口地址，留空则不调用 AI。</small>
          </div>
          <div class="field">
            <label for="LLM_MODEL">LLM_MODEL</label>
            <input id="LLM_MODEL" type="text" placeholder="deepseek-chat" />
            <small>模型名。</small>
          </div>
          <div class="field">
            <label for="LLM_KEY">LLM_KEY</label>
            <input id="LLM_KEY" type="password" autocomplete="off" />
            <small>API Key。</small>
            <div class="llm-test-row" style="margin-top:6px; display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
              <button type="button" onclick="testLLM()">测试 AI 连接</button>
              <span id="llm-test-result" class="llm-test-result"></span>
            </div>
          </div>
          <div class="field">
            <label for="LLM_MAX_TOKENS">LLM_MAX_TOKENS</label>
            <input id="LLM_MAX_TOKENS" type="number" placeholder="1024" />
            <small>单次回答最大 token。</small>
          </div>
          <div class="field">
            <label for="AI_PRIORITY">AI_PRIORITY</label>
            <input id="AI_PRIORITY" type="text" placeholder="1" />
            <small>1 = AI 优先答题；0 = 规则优先、AI 兜底。</small>
          </div>
          <div class="field">
            <label for="DELAY_PER_QUESTION">DELAY_PER_QUESTION</label>
            <input id="DELAY_PER_QUESTION" type="number" placeholder="2" />
            <small>答题间隔（秒），建议 ≥ 2 避免风控。</small>
          </div>
          <div class="field">
            <label for="MAX_ROUNDS">MAX_ROUNDS</label>
            <input id="MAX_ROUNDS" type="number" placeholder="10" />
            <small>未满分时最大重刷轮数。</small>
          </div>
          <div class="field">
            <label for="QUESTION_TIMEOUT">QUESTION_TIMEOUT</label>
            <input id="QUESTION_TIMEOUT" type="number" placeholder="60" />
            <small>单题超时秒数，超时自动跳过该题（防卡死）。</small>
          </div>
          <div class="field">
            <label for="USE_WORD_API">USE_WORD_API</label>
            <input id="USE_WORD_API" type="text" placeholder="1" />
            <small>1 = 用查词接口辅助答题；0 = 禁用（弱网提速，纯题库+AI+试探）。</small>
          </div>
          <div class="field">
            <label for="OCR_MODE">OCR_MODE</label>
            <input id="OCR_MODE" type="text" placeholder="off" />
            <small>图片题干/选项读图：off / llm（大模型视觉）/ tesseract（本地）/ baidu（百度OCR）。</small>
          </div>
          <div class="field">
            <label for="BAIDU_OCR_AK">BAIDU_OCR_AK</label>
            <input id="BAIDU_OCR_AK" type="text" placeholder="" />
            <small>百度 OCR API Key（OCR_MODE=baidu 时填）。</small>
          </div>
          <div class="field">
            <label for="BAIDU_OCR_SK">BAIDU_OCR_SK</label>
            <input id="BAIDU_OCR_SK" type="password" placeholder="" />
            <small>百度 OCR Secret Key。</small>
          </div>
        </div>
        <!-- 保存操作栏：重新读取 / 保存 / 保存并刷新任务 -->
        <div class="bar" style="margin-top:18px; margin-bottom:0; justify-content:space-between;">
          <div id="env-path" class="status-box">.env 路径加载中...</div>
          <div class="btn-row">
            <button onclick="loadConfig()">重新读取</button>
            <button class="primary" onclick="saveConfig(false)">保存配置</button>
            <button class="primary" onclick="saveConfig(true)">保存并刷新任务</button>
          </div>
        </div>
      </div>
    </section>

    <!-- ==================== 卡片 2：任务面板 ==================== -->
    <section class="card">
      <div class="card-head">
        <div><h2>任务面板</h2><p>子进程执行刷题流程，日志实时查看。重启程序后点「断点恢复」继续所有未完成。</p></div>
        <div class="status-box">分数每 5 秒自动刷新</div>
      </div>
      <div class="card-body">
        <div class="bar">
          <button onclick="loadTasks()">刷新任务</button>
          <button class="loop" onclick="startAllClassLoop()">断点恢复（继续未完成）</button>
          <button onclick="loadReports()">测试报告</button>
          <span id="status">加载中...</span>
        </div>
        <!-- 任务列表：PC 显示表格，手机自动转卡片（data-label 列名） -->
        <table>
          <thead><tr>
            <th style="width:40px">#</th><th>任务名</th><th style="width:160px">进度</th>
            <th style="width:80px">分数</th><th style="width:80px">状态</th><th style="width:230px">操作</th>
          </tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
    </section>

    <!-- ==================== 卡片 3：测试报告 ==================== -->
    <section class="card" id="report-card" style="display:none;">
      <div class="card-head">
        <div><h2>测试报告</h2><p>每题判分与来源统计（按任务聚合，越刷越有参考价值）。</p></div>
        <button onclick="loadReports()">刷新</button>
      </div>
      <div class="card-body" id="report-body"><div style="color:#667085;">加载中...</div></div>
    </section>
  </div>

  <!-- ==================== 弹窗：实时日志 ==================== -->
  <!-- 点击遮罩(非弹窗本体)关闭；手机端自动全屏 -->
  <div class="modal" id="modal-logs" onclick="if(event.target===this)closeLogs()">
    <div class="modal-body">
      <div class="modal-head"><span id="log-title">日志</span><button onclick="closeLogs()">✕ 关闭</button></div>
      <div class="modal-logs" id="log-content"></div>
    </div>
  </div>

  <!-- ==================== 弹窗：扫码登录 ==================== -->
  <div class="modal" id="modal-qr" onclick="if(event.target===this)closeQr()">
    <div class="modal-body qr-modal">
      <div class="modal-head"><span>微信扫码登录</span><button onclick="closeQr()">✕ 关闭</button></div>
      <div class="qr-box">
        <div id="qr-img-box" style="min-height:240px; display:flex; align-items:center; justify-content:center; color:#888;">生成中...</div>
        <div id="qr-status" style="font-size:13px; color:#666;">请使用微信扫一扫授权词达人</div>
      </div>
    </div>
  </div>

<script>
/* ============================================================
   全局状态
   - activeLogKey: 当前正在查看日志的任务（用于轮询 /api/logs）
   - logTimer:     日志自动刷新定时器句柄
   - currentTasks: 最近一次 /api/tasks 返回的任务数组（行内操作按下标取任务）
   - qrTimer:      扫码结果轮询定时器句柄
   - CONFIG_KEYS:  配置表单字段清单（与后端 get_runtime_config 的键一一对应）
   ============================================================ */
let activeLogKey = null;
let logTimer = null;
let currentTasks = [];
let qrTimer = null;
const CONFIG_KEYS = ["USERTOKEN","ABC","AUTH_V","COURSE_ID","STUDY_GRADE","LLM_URL","LLM_KEY","LLM_MODEL","LLM_MAX_TOKENS","AI_PRIORITY","DELAY_PER_QUESTION","MAX_ROUNDS","QUESTION_TIMEOUT","USE_WORD_API","OCR_MODE","BAIDU_OCR_AK","BAIDU_OCR_SK"];

/* ============================================================
   配置中心
   ============================================================ */

/**
 * loadConfig — 拉取后端当前配置(.env)，回填表单。
 * 同时更新：答案库统计胶囊、鉴权状态胶囊、.env 路径显示。
 */
async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "读取配置失败");
    // 把后端返回的每个字段写进对应 id 的 input
    for (const key of CONFIG_KEYS) {
      const el = document.getElementById(key);
      if (el) el.value = j.config[key] || "";
    }
    document.getElementById("env-path").textContent = "📄 " + j.env_file;
    renderStats(j.stats);
    renderConfigStatus(j.missing_auth || []);
  } catch (e) {
    // 读取失败时把状态胶囊变橙色，提示用户
    document.getElementById("config-pill").className = "status-box warn";
    document.getElementById("config-pill").textContent = "配置读取失败";
  }
}

/**
 * renderConfigStatus — 按缺失字段列表渲染右上角鉴权胶囊。
 * missing 为空 → 绿色"配置完整"；否则橙色列出缺的字段。
 */
function renderConfigStatus(missing) {
  const pill = document.getElementById("config-pill");
  if (!missing || missing.length === 0) {
    pill.className = "status-box ok";
    pill.textContent = "鉴权配置完整，可直接启动任务";
  } else {
    pill.className = "status-box warn";
    pill.textContent = "缺少字段: " + missing.join(", ");
  }
}

/**
 * renderStats — 渲染答案库统计（题库/短语/单词/词表/词典条目数）。
 * 词典为离线释义词典（ECDICT 开源数据），词义题离线直接命中。
 */
function renderStats(stats) {
  if (!stats) return;
  document.getElementById("stats-box").textContent =
    `答案库: 题库 ${stats.bank} · 短语 ${stats.phrase} · 单词 ${stats.word} · 词表 ${stats.pool} · 词典 ${stats.dict}`;
}

/**
 * testLLM — 用表单当前 LLM 配置（无需先保存）发一个最小请求，验证 AI 接口可用性。
 * 结果就地显示：成功绿字（含延迟与模型回复），失败红字（含具体原因）。
 */
async function testLLM() {
  const box = document.getElementById("llm-test-result");
  box.textContent = "测试中，请稍候...";
  box.className = "llm-test-result";
  try {
    const r = await fetch("/api/test_llm", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        llm_url: document.getElementById("LLM_URL").value.trim(),
        llm_key: document.getElementById("LLM_KEY").value.trim(),
        llm_model: document.getElementById("LLM_MODEL").value.trim(),
        llm_max_tokens: document.getElementById("LLM_MAX_TOKENS").value.trim(),
      }),
    });
    const j = await r.json();
    if (j.ok) {
      box.textContent = `✅ 连接成功 ${j.latency_ms}ms · 模型回复: ${j.reply}`;
      box.className = "llm-test-result ok";
    } else {
      box.textContent = `❌ ${j.error}`;
      box.className = "llm-test-result fail";
    }
  } catch (e) {
    box.textContent = `❌ 请求失败: ${e.message}`;
    box.className = "llm-test-result fail";
  }
}

/**
 * saveConfig — 把表单所有字段 POST 到后端保存为 .env。
 * @param refreshTasks true 时保存后顺手刷新任务面板（"保存并刷新任务"按钮）
 */
async function saveConfig(refreshTasks) {
  const payload = {};
  for (const key of CONFIG_KEYS) {
    const el = document.getElementById(key);
    payload[key] = el ? el.value : "";
  }
  const r = await fetch("/api/config", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const j = await r.json();
  if (!j.ok) { alert("保存失败: " + (j.error || "未知错误")); return; }
  renderConfigStatus(j.missing_auth || []);
  renderStats(j.stats);
  document.getElementById("env-path").textContent = "✅ 已保存到 " + j.env_file;
  if (refreshTasks) await loadTasks();
}

/**
 * checkToken — 调后端校验 USERTOKEN 是否有效，弹窗展示学生姓名或错误。
 */
async function checkToken() {
  const r = await fetch("/api/token_check");
  const j = await r.json();
  if (j.ok) alert("Token 有效: " + j.name);
  else alert(j.error || "Token 无效");
}

/* ============================================================
   扫码登录
   ============================================================ */

/**
 * openQr — 打开扫码弹窗并请求后端生成二维码。
 * 后端返回 base64 图片直接展示；不支持时给授权链接兜底。
 * 成功后每 3 秒轮询一次扫码结果（pollQr）。
 */
async function openQr() {
  document.getElementById("modal-qr").classList.add("show");
  document.getElementById("qr-status").textContent = "生成中...";
  try {
    const r = await fetch("/api/login_qr", { method: "POST" });
    const j = await r.json();
    if (!j.ok) { document.getElementById("qr-status").textContent = "失败: " + j.error; return; }
    const box = document.getElementById("qr-img-box");
    if (j.qr_base64) {
      box.innerHTML = `<img src="data:image/png;base64,${j.qr_base64}" />`;
    } else {
      box.innerHTML = `<div style="max-width:240px; word-break:break-all; font-size:12px; color:#007aff;"><a href="${escapeHtml(j.redirect_url)}" target="_blank">无法显示二维码，点此打开授权链接</a></div>`;
    }
    document.getElementById("qr-status").textContent = "请使用微信扫一扫授权词达人";
    if (qrTimer) clearInterval(qrTimer);
    qrTimer = setInterval(pollQr, 3000);
  } catch (e) {
    document.getElementById("qr-status").textContent = "失败: " + e.message;
  }
}

/**
 * pollQr — 轮询扫码结果。
 * 成功 → 停轮询、提示欢迎、自动刷新配置；等待中 → 提示等待；失败 → 停轮询。
 */
async function pollQr() {
  const r = await fetch("/api/login_poll");
  const j = await r.json();
  if (j.ok) {
    if (qrTimer) clearInterval(qrTimer);
    document.getElementById("qr-status").textContent = "✅ 授权成功: " + j.name;
    await loadConfig();
    setTimeout(closeQr, 1500);
  } else if (j.pending) {
    document.getElementById("qr-status").textContent = "等待扫码确认...";
  } else {
    if (qrTimer) clearInterval(qrTimer);
    document.getElementById("qr-status").textContent = j.error || "失败";
  }
}

/** closeQr — 关闭扫码弹窗并停掉轮询。 */
function closeQr() {
  if (qrTimer) clearInterval(qrTimer);
  document.getElementById("modal-qr").classList.remove("show");
}

/* ============================================================
   任务面板
   ============================================================ */

/**
 * loadTasks — 拉取 /api/tasks 渲染任务表格，并刷新答案库统计。
 * 页面上方"刷新任务"按钮与 5 秒定时器共用此函数。
 */
async function loadTasks() {
  document.getElementById("status").textContent = "拉取中...";
  try {
    const r = await fetch("/api/tasks");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error);
    render(j.tasks);
    renderStats(j.stats);
    const warnings = (j.warnings || []).length ? " · " + j.warnings.join(" · ") : "";
    document.getElementById("status").textContent = `共 ${j.tasks.length} 个任务 · ${new Date().toLocaleTimeString()}${warnings}`;
  } catch (e) {
    document.getElementById("status").textContent = "❌ " + e.message;
  }
}

/**
 * render — 把任务数组渲染进表格。
 * 每个 td 带 data-label（窄屏时 CSS 用它显示列名）。
 * 运行中的任务显示"停止/日志"，未运行显示"启动/循环"。
 */
function render(tasks) {
  currentTasks = tasks;
  const tb = document.getElementById("tbody");
  if (!tasks.length) {
    tb.innerHTML = `<tr><td colspan="6" style="color:#667085; text-align:center; padding:24px;">暂无任务，或当前账号下还没有可见任务。</td></tr>`;
    return;
  }
  tb.innerHTML = tasks.map((t, i) => {
    const prog = t.progress || 0;
    const score = t.score == null ? "-" : t.score;
    const scoreCls = t.score >= 100 ? "score full" : "score";
    let badge = `<span class="badge">${escapeHtml(t.source_label || "")}</span>`;
    if (t.running) {
      badge += t.loop ? ` <span class="badge run">循环中 (第${t.round}轮)</span>` : ` <span class="badge run">运行中</span>`;
    } else if (t.done) {
      badge += ` <span class="badge done">已完成</span>`;
    }
    return `<tr>
      <td data-label="#序号">${i}</td>
      <td data-label="任务名"><span class="cell-val">${escapeHtml(t.task_name)}</span></td>
      <td data-label="进度"><span class="cell-val"><div class="progress-bar"><div style="width:${prog}%"></div></div>${prog}%</span></td>
      <td data-label="分数" class="${scoreCls}"><span class="cell-val">${score}</span></td>
      <td data-label="状态"><span class="cell-val">${badge}</span></td>
      <td data-label="操作"><span class="cell-val"><span class="btn-wrap">${
        t.running
          ? `<button class="danger" onclick="stopTask(${i})">停止</button><button onclick="showLogs(${i})">日志</button>`
          : `<button class="primary" onclick="startTask(${i}, false)">▶ 启动</button><button class="loop" onclick="startTask(${i}, true)" title="刷到100分为止">🔁 循环</button>${t.done ? `<button onclick="showLogs(${i})">日志</button>` : ""}`
      }</span></span></td>
    </tr>`;
  }).join("");
}

/**
 * taskPayload — 把任务对象 + 循环标记拼成后端 /api/start 需要的请求体。
 */
function taskPayload(task, loop) {
  return { source: task.source || "class", task_id: task.task_id, release_id: task.release_id,
           course_id: task.course_id, list_id: task.list_id, task_type: task.task_type,
           grade: task.grade, loop: !!loop };
}

/**
 * startTask — 启动单个任务（loop=false 单轮 / loop=true 循环刷到满分）。
 * 启动成功后自动打开日志弹窗并刷新列表。
 */
async function startTask(index, loop) {
  const task = currentTasks[index];
  if (!task) return;
  const r = await fetch("/api/start", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(taskPayload(task, loop))
  });
  const j = await r.json();
  if (!j.ok) { alert("启动失败: " + j.error); return; }
  showLogs(index, loop);
  loadTasks();
}

/**
 * stopTask — 停止正在运行的任务（终止子进程，循环任务同时取消循环）。
 */
async function stopTask(index) {
  const task = currentTasks[index];
  if (!task) return;
  if (!confirm("停止任务?")) return;
  const r = await fetch("/api/stop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(taskPayload(task, false))
  });
  const j = await r.json();
  if (!j.ok) alert("停止失败: " + j.error);
  loadTasks();
}

/**
 * startAllClassLoop — 断点恢复：让后端把所有"未满分且未运行"的班级任务
 * 以循环模式启动。后端返回启动数/跳过数。
 */
async function startAllClassLoop() {
  if (!confirm("断点恢复：启动所有未满分班级任务（跳过运行中/已满分）？")) return;
  const r = await fetch("/api/start_all", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const j = await r.json();
  if (!j.ok) { alert("启动失败: " + j.error); return; }
  alert(`已启动 ${j.started} 个任务循环，跳过 ${j.skipped} 个（运行中/已满分）`);
  loadTasks();
}

/* ============================================================
   实时日志
   ============================================================ */

/**
 * showLogs — 打开日志弹窗，绑定 activeLogKey 并启动 1.5s 轮询。
 * @param loop 仅用于标题上显示 🔁 标识。
 */
async function showLogs(index, loop) {
  const task = currentTasks[index];
  if (!task) return;
  activeLogKey = taskPayload(task, false);
  document.getElementById("log-title").textContent = "📜 " + task.task_name + (loop ? " 🔁" : "");
  document.getElementById("modal-logs").classList.add("show");
  await refreshLogs();
  if (logTimer) clearInterval(logTimer);
  logTimer = setInterval(refreshLogs, 1500);
}

/** closeLogs — 关闭日志弹窗并停掉轮询。 */
function closeLogs() {
  activeLogKey = null;
  document.getElementById("modal-logs").classList.remove("show");
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

/**
 * renderLogLine — 把一行原始日志解析成带徽章的结构化行（HTML）。
 * 支持格式：
 *   [15] OK vt. 使分心；分散（注意力） [rule]      → 题型+状态+内容+来源
 *   [32]？答题 [llm]                               → 答题中
 *   [32] FIX a,historic,mission [corrects]         → 纠错入库
 *   [题 3/65] 开始 / [词表] 共 40 词 / [完成] 65/65 → 区块信息行
 * 无法识别的行降级为普通灰色文本（内容一律 escapeHtml，防注入）。
 * @param {string} line 原始日志行
 * @returns {string} 渲染后的 HTML 片段
 */
function renderLogLine(line) {
  const t = String(line || "").trim();
  if (!t) return "";
  const esc = escapeHtml(t);

  // 1) 题型答题行：先摘尾部来源 [rule]/[llm]/[corrects]...
  //    兼容两种日志形态：[15]? 答题（半角问号+空格）与 [32]？答题（全角问号紧贴）
  let m = t.match(/^(\[\d+\])(?:[?？]\s*|\s+)(.+?)\s+\[([a-z_]+)\]$/i);
  if (m) {
    const modeTag = m[1], body = m[2], src = m[3].toLowerCase();
    let badge = "", badgeCls = "";
    const s = body.toUpperCase();
    if (s.startsWith("OK"))       { badge = "对"; badgeCls = "ok"; }
    else if (s.startsWith("FIX")) { badge = "学"; badgeCls = "fix"; }
    else if (s.startsWith("SKIP")){ badge = "跳"; badgeCls = "err"; }
    else if (s.includes("答题") || body.startsWith("?")) { badge = "答"; badgeCls = "ask"; }
    else                          { badge = "题"; badgeCls = "ask"; }
    const srcLabel = { rule:"规则", llm:"AI", corrects:"纠错", cache:"题库", blank:"试探", skip:"跳过" }[src] || src;
    const badgeHtml = badge ? `<span class="lg-badge ${badgeCls}">${badge}</span>` : "";
    return `<div class="log-line"><span class="lg-mode">${modeTag}</span>${badgeHtml}<span class="lg-msg">${escapeHtml(body)}</span><span class="lg-src ${src}">${srcLabel}</span></div>`;
  }

  // 2) 区块信息行：[xxx] 内容（词表/选词/题 x/y/开始/完成/分数/满分/签到/网络异常...）
  m = t.match(/^\[([^\]]+)\]\s*(.*)$/);
  if (m) {
    const head = m[1], rest = m[2];
    let cls = "info";
    if (/^(网络异常|风控|失败|异常|保护|选词失败|跳过|超时|卡住|放弃)/.test(head)) cls = "err";
    else if (/^(满分|完成|签到)/.test(head)) cls = "ok";
    return `<div class="log-line"><span class="lg-tag ${cls}">${escapeHtml(head)}</span><span class="lg-msg ${cls === "err" ? "err" : "muted"}">${escapeHtml(rest)}</span></div>`;
  }

  // 3) 兜底：普通文本行
  return `<div class="log-line"><span class="lg-msg muted">${esc}</span></div>`;
}

/**
 * renderLogLines — 批量渲染日志行。
 * @param {string[]} lines 原始日志行数组
 * @returns {string} 全部行的 HTML
 */
function renderLogLines(lines) {
  return (lines || []).map(renderLogLine).join("");
}

/**
 * refreshLogs — 按 activeLogKey 拉取最新日志并渲染。
 * 若日志框原本在底部则自动跟随滚动到底（方便追新输出）；
 * 任务结束后停轮询并刷新任务列表。
 */
async function refreshLogs() {
  if (!activeLogKey) return;
  try {
    const qs = new URLSearchParams({ source: activeLogKey.source || "class", task_id: activeLogKey.task_id, release_id: activeLogKey.release_id });
    const r = await fetch(`/api/logs?${qs.toString()}`);
    const j = await r.json();
    if (j.ok) {
      const box = document.getElementById("log-content");
      const wasBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      box.innerHTML = renderLogLines(j.logs);
      if (wasBottom) box.scrollTop = box.scrollHeight;
      if (j.done) { clearInterval(logTimer); logTimer = null; loadTasks(); }
    }
  } catch (e) {}
}

/* ============================================================
   测试报告
   ============================================================ */

/**
 * loadReports — 拉取 /api/reports 渲染报告列表卡片。
 * 无报告时显示提示；有报告时展示 任务/总题/答对/跳过/正确率/来源分布。
 */
async function loadReports() {
  try {
    const r = await fetch("/api/reports");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "读取失败");
    const card = document.getElementById("report-card");
    card.style.display = "";
    const body = document.getElementById("report-body");
    if (!j.reports.length) {
      body.innerHTML = '<div style="color:#667085;">暂无报告，刷过题之后这里会显示每题判分统计。</div>';
      return;
    }
    body.innerHTML = `<table><thead><tr>
      <th>任务</th><th>总题</th><th>答对</th><th>跳过</th><th>正确率</th><th>来源分布</th><th style="width:70px">明细</th>
    </tr></thead><tbody>${
      j.reports.map((s, i) => {
        const dist = Object.entries(s.src_dist || {}).map(([k, v]) => `${k}:${v}`).join(" · ");
        return `<tr>
          <td data-label="任务"><span class="cell-val">${escapeHtml(s.key)}</span></td>
          <td data-label="总题"><span class="cell-val">${s.total}</span></td>
          <td data-label="答对"><span class="cell-val">${s.ok}</span></td>
          <td data-label="跳过"><span class="cell-val">${s.skip}</span></td>
          <td data-label="正确率"><span class="cell-val" style="color:${s.accuracy >= 90 ? "#34c759" : s.accuracy >= 60 ? "#ff9500" : "#ff3b30"}; font-weight:600">${s.accuracy}%</span></td>
          <td data-label="来源分布"><span class="cell-val" style="font-size:12px;color:#667085;">${escapeHtml(dist)}</span></td>
          <td data-label="明细"><span class="cell-val"><span class="btn-wrap"><button onclick="showReportDetail('${escapeHtml(s.key)}')">查看</button></span></span></td>
        </tr>`;
      }).join("")
    }</tbody></table>`;
  } catch (e) {
    alert("报告读取失败: " + e.message);
  }
}

/**
 * showReportDetail — 拉取单任务报告明细（最近 50 条），
 * 在报告卡片内切换为明细表格；顶部有"返回列表"按钮。
 * 同时 alert 弹窗给出汇总数字（任务/对/正确率/来源）。
 */
async function showReportDetail(key) {
  const r = await fetch(`/api/report?key=${encodeURIComponent(key)}`);
  const j = await r.json();
  if (!j.ok) { alert(j.error); return; }
  const st = j.stats;
  const dist = Object.entries(st.src_dist || {}).map(([k, v]) => `${k}:${v}`).join(" · ");
  const rows = (j.recs || []).slice(-50).reverse().map((rec) => {
    const tag = rec.ok ? '<span style="color:#34c759">OK</span>' : '<span style="color:#ff3b30">✗</span>';
    return `<tr><td data-label="题型"><span class="cell-val" style="font-size:11px;color:#666">${rec.mode}</span></td><td data-label="来源"><span class="cell-val">${escapeHtml(rec.src)}</span></td><td data-label="结果"><span class="cell-val">${tag}</span></td><td data-label="题干"><span class="cell-val" style="font-size:12px">${escapeHtml(rec.stem)}</span></td><td data-label="备注"><span class="cell-val" style="font-size:12px;color:#667085">${escapeHtml(rec.remark)}</span></td></tr>`;
  }).join("");
  alert(`[${st.key}] 共${st.total}题 · 对${st.ok} · 正确率${st.accuracy}%
来源: ${dist}`);
  if (rows) {
    const card = document.getElementById("report-card");
    const body = document.getElementById("report-body");
    body.innerHTML = `<div class="bar" style="margin-bottom:10px;"><button onclick="loadReports()">← 返回列表</button><span>最近 50 条明细</span></div>` +
      `<table><thead><tr><th>题型</th><th>来源</th><th>结果</th><th>题干</th><th>备注</th></tr></thead><tbody>${rows}</tbody></table>`;
  }
}

/* ============================================================
   工具函数
   ============================================================ */

/**
 * escapeHtml — 转义 HTML 特殊字符，防止任务名等用户数据
 * 注入页面（XSS 防护）。所有插入 innerHTML 的文本都应过一遍。
 */
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

/* ============================================================
   启动
   - 页面加载：先读配置 → 拉任务 → 拉报告
   - 之后每 5 秒自动刷新任务列表（分数/状态实时更新）
   ============================================================ */
loadConfig().then(loadTasks).then(loadReports);
setInterval(loadTasks, 5000);
</script>
</body>
</html>
'''


def main():
    # Termux / 局域网适配：CIDAREN_HOST / CIDAREN_PORT 环境变量可覆盖监听地址
    host = os.environ.get("CIDAREN_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("CIDAREN_PORT", "5000"))
    except (TypeError, ValueError):
        port = 5000
    print(f"cidaren-ultimate Web 控制台: http://{host}:{port}")
    print(f"配置文件: {env_file_path()}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
