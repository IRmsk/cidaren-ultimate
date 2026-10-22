# -*- coding: utf-8 -*-
"""
mock_server.py — 模拟词达人服务端（供 E2E 测试，整合 xuhaoranbb-boop 的 mock 思路）
启动后通过环境变量 CIDAREN_API_BASE 指向本服务，即可在无真实账号情况下验证答题引擎：
  选词 -> StartAnswer -> 逐题 Verify/Submit -> 满分 -> 循环重刷
判分规则与真实服务端一致：答对 answer_result=1；答错/空白返回 answer_corrects 标准答案。
"""
from __future__ import annotations

import json
import threading

from flask import Flask, jsonify, request

app = Flask(__name__)
LOCK = threading.Lock()

TOTAL = 16
MOCK_WORDS = [
    {"word": "green", "word_zh": "绿色"},
    {"word": "storage", "word_zh": "存储"},
    {"word": "accomplish", "word_zh": "完成"},
    {"word": "responsible", "word_zh": "负责的"},
    {"word": "conscious", "word_zh": "有意识的"},
    {"word": "manufacturing", "word_zh": "制造"},
    {"word": "instance", "word_zh": "实例"},
    {"word": "minister", "word_zh": "部长"},
    {"word": "withstand", "word_zh": "经受"},
    {"word": "pursue", "word_zh": "追求"},
]
SESSION = {}
TASKS = {}  # task_id -> {"release_id", "score"}，供 PageTask 列表返回


def _gen_questions():
    """固定 mock 题库，循环到 TOTAL 题，覆盖全部 19 种题型。"""
    bank = [
        # (mode, stem, remark, options, answer, extra)
        # === 英译汉类（11/15/16/21/22）===
        (11, "He will spoil rather than {accomplish} things.", "vt. 完成；达成",
         [{"content": "vt. 完成；达成"}, {"content": "vt. 破坏"}, {"content": "vt. 追求"}, {"content": "vt. 经受住"}], 0, {}),
        (15, "The boxes can {withstand} massive force.", "vt. 经受住；顶住",
         [{"content": "vt. 经受住"}, {"content": "vt. 解决"}, {"content": "vt. 追赶"}, {"content": "vt. 使破产"}], 0, {}),
        (16, "She is {conscious} of the risk.", "adj. 有意识的",
         [{"content": "adj. 有意识的"}, {"content": "adj. 负责的"}, {"content": "adj. 绿色的"}, {"content": "adj. 存储的"}], 0, {}),
        (21, "This is an {instance} of success.", "n. 实例",
         [{"content": "n. 实例"}, {"content": "n. 部长"}, {"content": "n. 绿色"}, {"content": "n. 存储"}], 0, {}),
        (22, "He is the {minister} of culture.", "n. 部长",
         [{"content": "n. 部长"}, {"content": "n. 实例"}, {"content": "n. 绿色"}, {"content": "n. 存储"}], 0, {}),
        
        # === 看义选词类（17/18）===
        (17, "绿色", "选择正确的单词",
         [{"content": "accomplish"}, {"content": "storage"}, {"content": "green"}, {"content": "pursue"}], 2, {}),
        (18, "部长", "选择正确的单词",
         [{"content": "minister"}, {"content": "instance"}, {"content": "pursue"}, {"content": "green"}], 0, {}),
        
        # === 搭配题（31）===
        (31, "green", "搭配",
         [{"content": "in"}, {"content": "out"}, {"content": "up"}, {"content": "off"}], [0], {"answer_num": 1}),
        (31, "storage", "搭配",
         [{"content": "on"}, {"content": "off"}, {"content": "in"}, {"content": "out"}], [1], {"answer_num": 1}),
        
        # === 汉译英填空（32）===
        (32, "___ _", "绿色制造",
         [{"content": "green"}, {"content": "manufacturing"}, {"content": "the"}], "green,manufacturing", {}),
        (32, "___ ___", "负责的公民",
         [{"content": "responsible"}, {"content": "citizen"}, {"content": "the"}], "responsible,citizen", {}),
        
        # === 选词完成句子（41/42/43/44）===
        (41, "He is {} for the task.", "负责的",
         [{"content": "storage"}, {"content": "responsible"}, {"content": "green"}, {"content": "instance"}], 1, {}),
        (42, "She is {} of the risk.", "有意识的",
         [{"content": "conscious"}, {"content": "green"}, {"content": "pursue"}, {"content": "storage"}], 0, {}),
        (43, "This is an {} of success.", "实例",
         [{"content": "instance"}, {"content": "green"}, {"content": "pursue"}, {"content": "storage"}], 0, {}),
        (44, "He is the {} of culture.", "部长",
         [{"content": "minister"}, {"content": "green"}, {"content": "pursue"}, {"content": "storage"}], 0, {}),
        
        # === 完成句子（51/52/53/54）===
        (51, "{}een is a color.", "绿色",
         [{"content": "green"}, {"content": "great"}, {"content": "grow"}], "green", {"w_lens": [5], "w_tip": "gr"}),
        (52, "{}ist is a person.", "部长",
         [{"content": "minister"}, {"content": "manage"}, {"content": "master"}], "minister", {"w_lens": [8], "w_tip": "mi"}),
        (53, "{}onscious is an adjective.", "有意识的",
         [{"content": "conscious"}, {"content": "conscience"}, {"content": "consent"}], "conscious", {"w_lens": [9], "w_tip": "co"}),
        (54, "{}tance is a noun.", "实例",
         [{"content": "instance"}, {"content": "instant"}, {"content": "instead"}], "instance", {"w_lens": [8], "w_tip": "in"}),
        
        # === 单词拼写（73）===
        (73, "{gr}{sto}", "绿色存储",
         [], ["green", "storage"], {"w_lens": [5, 7]}),
        (73, "{res}{cit}", "负责的公民",
         [], ["responsible", "citizen"], {"w_lens": [10, 6]}),
        
        # === 例句配对（13）===
        (13, "He is a man who will spoil rather than accomplish things.", "vt. 完成",
         [{"content": "accomplish"}, {"content": "withstand"}, {"content": "minister"}, {"content": "green"}], 0, {}),
        
        # === 学习卡片（0）===
        (0, "green", "学习卡片",
         [{"content": "adj 绿色的"}, {"content": "n 绿色"}], None, {}),
        (0, "storage", "学习卡片",
         [{"content": "n 存储"}], None, {}),
    ]
    out = []
    for i in range(TOTAL):
        q = bank[i % len(bank)]
        out.append(q)
    return out


def _state(task_id, release_id):
    key = f"{task_id}-{release_id}"
    with LOCK:
        if key not in SESSION:
            SESSION[key] = {
                "questions": _gen_questions(),
                "done": 0,
                "correct": 0,
                "score": 0,
                "selected": False,
            }
        if task_id in TASKS:
            TASKS[task_id]["score"] = SESSION[key]["score"]  # 保留首次登记的 release_id
        else:
            TASKS[task_id] = {"release_id": release_id, "score": SESSION[key]["score"]}
        return SESSION[key]


def _topic(st, task_id, seq, extra_meta=False):
    """构造第 seq 题（1-based）。seq=TOTAL 之后返回完成态。"""
    if seq > len(st["questions"]):
        return None
    mode, stem, remark, options, answer, extra = st["questions"][seq - 1]
    topic = {
        "topic_code": f"topic-{task_id}-{seq}",
        "topic_mode": mode,
        "topic_done_num": st["done"],
        "topic_total": len(st["questions"]),
        "stem": {"content": stem, "remark": remark},
        "options": options,
    }
    topic.update(extra)
    return topic, answer


@app.route("/studentv1/api/Student/Main")
def main():
    return jsonify({"code": 1, "data": {"user_info": {"student_name": "mock-student"}}})


@app.route("/studentv1/api/Student/ClassTask/PageTask", methods=["POST"])
def page_task():
    recs = []
    with LOCK:
        for tid, info in TASKS.items():
            recs.append({
                "task_id": tid, "release_id": info["release_id"],
                "task_name": f"mock-CET4-{tid}",
                "score": info["score"], "progress": info["score"], "task_type": 1,
            })
    return jsonify({"code": 1, "data": {"records": recs}})


@app.route("/studentv1/api/Student/ClassTask/ChoseWordList")
def chose_word_list():
    return jsonify({"code": 1, "data": {"word_list": MOCK_WORDS}})


@app.route("/studentv1/api/Student/ClassTask/SubmitChoseWord", methods=["POST"])
def submit_chose_word():
    # 仅确认勾选，不登记任务（任务登记统一走 StartAnswer，带正确 release_id）
    return jsonify({"code": 1})


@app.route("/studentv1/api/Student/ClassTask/StartAnswer")
def start_answer():
    body = request.args
    tid = int(body.get("task_id") or 1)
    st = _state(tid, int(body.get("release_id") or 1))
    t = _topic(st, tid, st["done"] + 1)
    if t is None:
        return jsonify({"code": 1, "data": {"topic_done_num": len(st["questions"]),
                                            "topic_total": len(st["questions"])}})
    topic, answer = t
    with LOCK:
        st["_ans"] = answer
    return jsonify({"code": 1, "data": topic})


@app.route("/studentv1/api/Student/ClassTask/VerifyAnswer", methods=["POST"])
def verify_answer():
    body = request.get_json(force=True)
    code = body.get("topic_code", "")
    answer = body.get("answer")
    tid = int(code.split("-")[1])
    st = _state(tid, 1)
    seq = int(code.split("-")[2])
    t = _topic(st, tid, seq)
    if t is None:
        return jsonify({"code": 1, "data": {"answer_result": 0}})
    _, correct = t
    if _match(answer, correct, st["questions"][seq - 1][0]):
        with LOCK:
            st["correct"] += 1
        return jsonify({"code": 1, "data": {"answer_result": 1, "topic_code": code, "over_status": 0}})
    return jsonify({"code": 1, "data": {"answer_result": 0, "topic_code": code,
                                        "answer_corrects": list(correct) if isinstance(correct, list) else [correct],
                                        "over_status": 0}})


def _match(answer, correct, mode) -> bool:
    if mode == 73:
        try:
            return json.loads(answer) == correct
        except (ValueError, TypeError):
            return answer == ",".join(correct)
    if mode == 32:
        return str(answer).strip() == str(correct).strip()
    if isinstance(correct, list):
        # 搭配/多选：answer 可以是单个 tag 或 tag 列表
        if isinstance(answer, list):
            return all(a in correct for a in answer)
        return answer in correct
    return answer == correct


@app.route("/studentv1/api/Student/ClassTask/SubmitAnswerAndSave", methods=["POST"])
def submit_and_save():
    body = request.get_json(force=True)
    code = body.get("topic_code", "")
    tid = int(code.split("-")[1])
    st = _state(tid, 1)
    seq = int(code.split("-")[2])
    with LOCK:
        st["done"] = min(st["done"] + 1, len(st["questions"]))
        if st["done"] >= len(st["questions"]):
            st["score"] = round(st["correct"] / len(st["questions"]) * 100, 1)
            if tid in TASKS:
                TASKS[tid]["score"] = st["score"]
    if st["done"] >= len(st["questions"]):
        return jsonify({"code": 1, "data": {"topic_done_num": st["done"],
                                            "topic_total": len(st["questions"])}})
    t = _topic(st, tid, st["done"] + 1)
    topic, answer = t
    with LOCK:
        st["_ans"] = answer
    return jsonify({"code": 1, "data": topic})


@app.route("/studentv1/api/Student/TaskStudentSignin/Do", methods=["POST"])
def signin():
    return jsonify({"code": 1, "data": {"sign_in_total": 3, "integral": 5}})


def run_mock_server(port: int = 0) -> int:
    """在线程里启动 mock server，返回实际端口。"""
    import socket
    if not port:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
    threading.Thread(target=lambda: app.run(host="127.0.0.1", port=port, debug=False,
                                            threaded=True, use_reloader=False), daemon=True).start()
    return port


if __name__ == "__main__":
    port = run_mock_server(5001)
    print(f"mock server: http://127.0.0.1:{port}/studentv1/api")
    import time
    while True:
        time.sleep(1)
