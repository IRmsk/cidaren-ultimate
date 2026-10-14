# -*- coding: utf-8 -*-
"""
web.py — Web 控制台（基于 LangQi99/cidaren 增强）
- 配置中心：鉴权 / 自学 / LLM / AI 策略 / 答题节奏
- 扫码登录（Rain-shadow/cdr 思路）：弹窗二维码，授权成功自动回填 USERTOKEN
- 答案库统计：题库 / 短语库 / 单词库 条目数实时展示
- 任务面板：班级 + 自学任务，启动 / 循环 / 停止 / 实时日志
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
from .config import (
    build_subprocess_env,
    env_file_path,
    get_missing_auth_fields,
    get_runtime_config,
    save_runtime_config,
)

app = Flask(__name__)

JOBS = {}
JOBS_LOCK = threading.Lock()
LOG_MAX = 800

# 扫码登录会话
QR_STATE = {"token": None, "started": 0.0}
QR_LOCK = threading.Lock()


def _job_key(source, task_id, release_id):
    return ((source or "class"), str(task_id), str(release_id))


def _int_or_default(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _find_job(source, task_id, release_id):
    job = JOBS.get(_job_key(source, task_id, release_id))
    if job or source != "study":
        return job
    for (job_source, _tid, job_rid), candidate in JOBS.items():
        if job_source == "study" and job_rid == str(release_id):
            return candidate
    return None


def _client(config=None):
    from .api import Client
    cfg = config or get_runtime_config()
    missing = get_missing_auth_fields(cfg)
    if missing:
        raise ValueError(f"请先在配置面板填写: {', '.join(missing)}")
    return Client(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))


def _list_class_tasks(c):
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
    resp = c.study_task_list(course_id=course_id)
    data = resp.get("data") or {}
    return data.get("task_list") or []


def _list_tasks():
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


# ================= Routes =================

@app.route("/")
def index():
    return Response(_INDEX_HTML, mimetype="text/html; charset=utf-8")


@app.route("/api/config")
def api_config():
    config = get_runtime_config()
    return jsonify({
        "ok": True, "config": config, "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(config),
        "stats": bk.lib_stats(),
    })


@app.route("/api/config", methods=["POST"])
def api_save_config():
    body = request.get_json(force=True) or {}
    saved = save_runtime_config(body)
    return jsonify({
        "ok": True, "config": saved, "env_file": env_file_path(),
        "missing_auth": get_missing_auth_fields(saved),
        "stats": bk.lib_stats(),
    })


@app.route("/api/tasks")
def api_tasks():
    try:
        tasks, warnings = _list_tasks()
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "tasks": tasks, "warnings": warnings, "stats": bk.lib_stats()})


@app.route("/api/token_check")
def api_token_check():
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
    """生成扫码登录二维码。"""
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
    """轮询扫码结果。成功后把 USERTOKEN 写进配置。"""
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


@app.route("/api/logs")
def api_logs_query():
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
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>cidaren-ultimate 控制台</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif;
         margin: 0; padding: 24px; background: #f5f7fb; color: #1d1d1f; }
  h1 { font-size: 26px; margin: 0 0 8px; }
  .subtitle { margin: 0 0 20px; color: #667085; font-size: 14px; }
  .layout { display: grid; gap: 20px; }
  .card { background: #fff; border: 1px solid #eaecf0; border-radius: 16px; box-shadow: 0 8px 24px rgba(15,23,42,.04); overflow: hidden; }
  .card-head { padding: 18px 20px; border-bottom: 1px solid #f2f4f7; display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  .card-head h2 { margin: 0; font-size: 18px; }
  .card-head p { margin: 4px 0 0; color: #667085; font-size: 13px; }
  .card-body { padding: 20px; }
  .bar { display: flex; gap: 12px; align-items: center; margin-bottom: 16px; font-size: 13px; color: #666; flex-wrap: wrap; }
  button { font: inherit; padding: 6px 14px; border-radius: 6px; border: 1px solid #d2d2d7; background: white; cursor: pointer; }
  button:hover { background: #f0f0f0; }
  button.primary { background: #007aff; color: white; border-color: #007aff; }
  button.primary:hover { background: #0062cc; }
  button.danger { background: #ff3b30; color: white; border-color: #ff3b30; }
  button.loop { background: #5856d6; color: white; border-color: #5856d6; }
  button.loop:hover { background: #4845b0; }
  button.green { background: #34c759; color: white; border-color: #34c759; }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .btn-row { display: flex; gap: 10px; flex-wrap: wrap; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(280px, 1fr)); gap: 16px; }
  .field { display: flex; flex-direction: column; gap: 8px; }
  .field label { font-size: 13px; font-weight: 600; color: #344054; }
  .field input { width: 100%; border: 1px solid #d0d5dd; border-radius: 10px; padding: 10px 12px; font: inherit; }
  .field small { color: #667085; font-size: 12px; }
  .full { grid-column: 1 / -1; }
  .status-box { display: inline-flex; align-items: center; gap: 8px; padding: 8px 12px; border-radius: 999px; background: #f8fafc; border: 1px solid #e2e8f0; color: #475467; font-size: 12px; }
  .status-box.ok { background: #ecfdf3; color: #027a48; border-color: #d1fadf; }
  .status-box.warn { background: #fff7ed; color: #b54708; border-color: #fed7aa; }
  .inline-code { font-family: ui-monospace, Menlo, monospace; background: #f2f4f7; padding: 2px 6px; border-radius: 6px; }
  table { width: 100%; border-collapse: collapse; background: white; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,.06); }
  th, td { padding: 10px 12px; text-align: left; border-bottom: 1px solid #f0f0f0; font-size: 14px; }
  th { background: #fafafa; font-weight: 600; color: #666; font-size: 12px; text-transform: uppercase; }
  tr:last-child td { border-bottom: none; }
  .progress-bar { width: 100px; height: 6px; background: #e8e8ed; border-radius: 3px; overflow: hidden; display: inline-block; vertical-align: middle; margin-right: 6px; }
  .progress-bar > div { height: 100%; background: #34c759; transition: width .3s; }
  .score { font-weight: 600; }
  .score.full { color: #34c759; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 500; }
  .badge.run { background: #fff3cd; color: #856404; }
  .badge.done { background: #d4edda; color: #155724; }
  .stats-row { display: flex; gap: 14px; flex-wrap: wrap; }
  .stat-pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; background: #eef2ff; color: #4338ca; font-size: 12px; }
  .modal { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.5); z-index: 100; }
  .modal.show { display: flex; align-items: center; justify-content: center; }
  .modal-body { background: #1e1e1e; color: #d4d4d4; width: 80vw; height: 75vh; border-radius: 8px; padding: 16px; overflow: hidden; display: flex; flex-direction: column; }
  .modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; color: white; }
  .modal-logs { flex: 1; overflow-y: auto; font-family: ui-monospace, Menlo, monospace; font-size: 12px; white-space: pre-wrap; word-break: break-word; line-height: 1.5; }
  .qr-box { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 24px; }
  .qr-box img { width: 240px; height: 240px; border-radius: 8px; }
  @media (max-width: 900px) {
    body { padding: 16px; }
    .grid { grid-template-columns: 1fr; }
    table { display: block; overflow-x: auto; }
  }
</style>
</head>
<body>
  <h1>cidaren-ultimate</h1>
  <p class="subtitle">词达人全自动刷题整合版：班级 + 自学任务 · 全题型规则引擎 · AI 优先答题 · 答案库自学习 · 扫码登录</p>

  <div class="layout">
    <section class="card">
      <div class="card-head">
        <div><h2>配置中心</h2><p>token / LLM / AI 策略，保存后写入 <span class="inline-code">.env</span>。</p></div>
        <div id="config-pill" class="status-box">读取中...</div>
      </div>
      <div class="card-body">
        <div class="bar">
          <button class="green" onclick="openQr()">微信扫码登录</button>
          <button onclick="checkToken()">校验 Token</button>
          <div id="stats-box" class="status-box">答案库: -</div>
        </div>
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
        </div>
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

    <section class="card">
      <div class="card-head">
        <div><h2>任务面板</h2><p>子进程执行刷题流程，日志实时查看。</p></div>
        <div class="status-box">分数每 5 秒自动刷新</div>
      </div>
      <div class="card-body">
        <div class="bar">
          <button onclick="loadTasks()">刷新任务</button>
          <button class="loop" onclick="startAllClassLoop()">一键启动全部班级循环</button>
          <span id="status">加载中...</span>
        </div>
        <table>
          <thead><tr>
            <th style="width:40px">#</th><th>任务名</th><th style="width:160px">进度</th>
            <th style="width:80px">分数</th><th style="width:80px">状态</th><th style="width:210px">操作</th>
          </tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
    </section>
  </div>

  <div class="modal" id="modal-logs" onclick="if(event.target===this)closeLogs()">
    <div class="modal-body">
      <div class="modal-head"><span id="log-title">日志</span><button onclick="closeLogs()">✕ 关闭</button></div>
      <div class="modal-logs" id="log-content"></div>
    </div>
  </div>

  <div class="modal" id="modal-qr" onclick="if(event.target===this)closeQr()">
    <div class="modal-body" style="width:360px; height:auto; background:#fff; color:#1d1d1f;">
      <div class="modal-head"><span>微信扫码登录</span><button onclick="closeQr()">✕ 关闭</button></div>
      <div class="qr-box">
        <div id="qr-img-box" style="min-height:240px; display:flex; align-items:center; justify-content:center; color:#888;">生成中...</div>
        <div id="qr-status" style="font-size:13px; color:#666;">请使用微信扫一扫授权词达人</div>
      </div>
    </div>
  </div>

<script>
let activeLogKey = null;
let logTimer = null;
let currentTasks = [];
let qrTimer = null;
const CONFIG_KEYS = ["USERTOKEN","ABC","AUTH_V","COURSE_ID","STUDY_GRADE","LLM_URL","LLM_KEY","LLM_MODEL","LLM_MAX_TOKENS","AI_PRIORITY","DELAY_PER_QUESTION","MAX_ROUNDS"];

async function loadConfig() {
  try {
    const r = await fetch("/api/config");
    const j = await r.json();
    if (!j.ok) throw new Error(j.error || "读取配置失败");
    for (const key of CONFIG_KEYS) {
      const el = document.getElementById(key);
      if (el) el.value = j.config[key] || "";
    }
    document.getElementById("env-path").textContent = "📄 " + j.env_file;
    renderStats(j.stats);
    renderConfigStatus(j.missing_auth || []);
  } catch (e) {
    document.getElementById("config-pill").className = "status-box warn";
    document.getElementById("config-pill").textContent = "配置读取失败";
  }
}

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

function renderStats(stats) {
  if (!stats) return;
  document.getElementById("stats-box").textContent =
    `答案库: 题库 ${stats.bank} · 短语 ${stats.phrase} · 单词 ${stats.word} · 词表 ${stats.pool}`;
}

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

async function checkToken() {
  const r = await fetch("/api/token_check");
  const j = await r.json();
  if (j.ok) alert("Token 有效: " + j.name);
  else alert(j.error || "Token 无效");
}

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

function closeQr() {
  if (qrTimer) clearInterval(qrTimer);
  document.getElementById("modal-qr").classList.remove("show");
}

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
      <td>${i}</td>
      <td>${escapeHtml(t.task_name)}</td>
      <td><div class="progress-bar"><div style="width:${prog}%"></div></div>${prog}%</td>
      <td class="${scoreCls}">${score}</td>
      <td>${badge}</td>
      <td>${
        t.running
          ? `<button class="danger" onclick="stopTask(${i})">停止</button><button onclick="showLogs(${i})">日志</button>`
          : `<button class="primary" onclick="startTask(${i}, false)">▶ 启动</button><button class="loop" onclick="startTask(${i}, true)" title="刷到100分为止">🔁 循环</button>${t.done ? `<button onclick="showLogs(${i})">日志</button>` : ""}`
      }</td>
    </tr>`;
  }).join("");
}

function taskPayload(task, loop) {
  return { source: task.source || "class", task_id: task.task_id, release_id: task.release_id,
           course_id: task.course_id, list_id: task.list_id, task_type: task.task_type,
           grade: task.grade, loop: !!loop };
}

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

async function startAllClassLoop() {
  if (!confirm("确定要一键启动所有班级任务循环（刷到满分）？")) return;
  const r = await fetch("/api/start_all", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  const j = await r.json();
  if (!j.ok) { alert("启动失败: " + j.error); return; }
  alert(`已启动 ${j.started} 个任务循环，跳过 ${j.skipped} 个（运行中/已满分）`);
  loadTasks();
}

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

function closeLogs() {
  activeLogKey = null;
  document.getElementById("modal-logs").classList.remove("show");
  if (logTimer) { clearInterval(logTimer); logTimer = null; }
}

async function refreshLogs() {
  if (!activeLogKey) return;
  try {
    const qs = new URLSearchParams({ source: activeLogKey.source || "class", task_id: activeLogKey.task_id, release_id: activeLogKey.release_id });
    const r = await fetch(`/api/logs?${qs.toString()}`);
    const j = await r.json();
    if (j.ok) {
      const box = document.getElementById("log-content");
      const wasBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 30;
      box.textContent = j.logs.join("\n");
      if (wasBottom) box.scrollTop = box.scrollHeight;
      if (j.done) { clearInterval(logTimer); logTimer = null; loadTasks(); }
    }
  } catch (e) {}
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
}

loadConfig().then(loadTasks);
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
