# -*- coding: utf-8 -*-
"""轮询任务直到结束，打印最终分数（本地部署监控用）"""
import json, sys, time, urllib.request

BASE = "http://127.0.0.1:5000"
TOK = "cidaren2026"
TID, RID = int(sys.argv[1]), int(sys.argv[2])

def get(path):
    """单次超时/网络抖动不终止监控，重试 3 次后返回 None。"""
    for _ in range(3):
        try:
            req = urllib.request.Request(BASE + path, headers={"X-Cidaren-Token": TOK})
            return json.load(urllib.request.urlopen(req, timeout=45))
        except Exception:
            time.sleep(5)
    return None

last = None
deadline = time.time() + 2400
while time.time() < deadline:
    data = get("/api/tasks")
    if not data:
        print("poll failed, retrying...", flush=True)
        continue
    for t in data["tasks"]:
        if t["task_id"] == TID and str(t["release_id"]) == str(RID):
            cur = (t["progress"], round(t["score"], 1), t["running"], t["done"])
            if cur != last:
                print("progress=%s%% score=%s running=%s done=%s" % cur, flush=True)
                last = cur
            if t["done"] and not t["running"]:
                print("FINISHED score=%s progress=%s%%" % (t["score"], t["progress"]), flush=True)
                sys.exit(0)
    time.sleep(15)
print("WATCH_TIMEOUT", flush=True)
