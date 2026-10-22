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

from flask import Flask, jsonify, request, Response, render_template

from core import bank as bk
from solver import report as rp
from core import ai as ai_mod
from core.config import (
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

# ------------------------------------------------------------------
# Web 面板访问控制
#   绑定 127.0.0.1 时无需密码（仅本地可访问）
#   绑定非本地地址时强制密码（CIDAREN_PASSWORD 环境变量，未设置则自动生成随机密码并打印）
# ------------------------------------------------------------------
_WEB_PASSWORD = ""
_WEB_AUTH_ENABLED = False


@app.before_request
def _check_web_auth():
    """非本地绑定时所有请求需带密码（query 参数 ?token= 或 Header X-Cidaren-Token）。"""
    if not _WEB_AUTH_ENABLED:
        return None
    token = request.args.get("token") or request.headers.get("X-Cidaren-Token", "")
    if token != _WEB_PASSWORD:
        return Response("Unauthorized: 请在地址后加 ?token=你的面板密码\n", status=401)
    return None


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
    from core.api import Client
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
        [sys.executable, "-u", "__main__.py", *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        bufsize=1, env=build_subprocess_env(config),
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
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
    return render_template('index.html')


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
    # 如果 USERTOKEN 变了，自动更新 TOKEN_SAVED_AT
    old_cfg = get_runtime_config()
    if body.get("USERTOKEN") and body.get("USERTOKEN") != old_cfg.get("USERTOKEN"):
        import time
        body["TOKEN_SAVED_AT"] = str(time.time())
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
    result = ai_mod.check_llm_connectivity(llm_url, llm_key, llm_model, max_tokens)
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
    from core.login import is_token_valid
    from core.config import token_expiry_hint
    cfg = get_runtime_config()
    missing = get_missing_auth_fields(cfg)
    if missing:
        return jsonify({"ok": False, "error": f"缺少 USERTOKEN"}), 400
    ok, info = is_token_valid(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))
    hint = token_expiry_hint()
    if ok:
        name = (info or {}).get("student_name") or (info or {}).get("nick_name") or "?"
        return jsonify({"ok": True, "name": name, "hint": hint})
    return jsonify({"ok": False, "error": "Token 无效或已过期", "hint": hint})


@app.route("/api/login_qr", methods=["POST"])
def api_login_qr():
    """生成扫码登录二维码。返回 user_token / base64 图片 / 授权链接。"""
    from core.login import qr_authorize
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
    from core.login import is_token_valid
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

def main():
    global _WEB_PASSWORD, _WEB_AUTH_ENABLED
    # Termux / 局域网适配：CIDAREN_HOST / CIDAREN_PORT 环境变量可覆盖监听地址
    host = os.environ.get("CIDAREN_HOST", "127.0.0.1")
    try:
        port = int(os.environ.get("CIDAREN_PORT", "5000"))
    except (TypeError, ValueError):
        port = 5000

    # 非本地绑定强制密码：防止局域网内任何人都能操作面板
    if host not in ("127.0.0.1", "localhost", "::1"):
        _WEB_AUTH_ENABLED = True
        _WEB_PASSWORD = os.environ.get("CIDAREN_PASSWORD", "")
        if not _WEB_PASSWORD:
            import secrets
            _WEB_PASSWORD = secrets.token_urlsafe(12)
            print(f"[安全] 未设置 CIDAREN_PASSWORD，已自动生成随机面板密码: {_WEB_PASSWORD}")
            print(f"[安全] 访问方式: http://{host}:{port}/?token={_WEB_PASSWORD}")
        else:
            print(f"[安全] 面板密码已从 CIDAREN_PASSWORD 读取")
        print(f"[安全] 如需关闭密码验证，请绑定 127.0.0.1 并通过 SSH 端口转发访问")

    print(f"cidaren-ultimate Web 控制台: http://{host}:{port}")
    print(f"配置文件: {env_file_path()}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
