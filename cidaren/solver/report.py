# -*- coding: utf-8 -*-
"""
report.py — 测试报告（整合 chuyc11/cidaren-yijian-shuati 的报告思路）
每次答题记录一行，按任务聚合统计，供 Web 控制台复盘：
  总题数 / 答对 / 跳过 / 正确率 / 答案来源分布（cache/ai/rule/blank/skip）
数据存 cidaren/data/reports/<task_key>.jsonl
"""
from __future__ import annotations

import json
import time
from pathlib import Path

REPORTS_DIR = Path(__file__).resolve().parent / "data" / "reports"

REPORT_FIELDS = ("mode", "src", "ok", "stem", "remark", "answer")


def task_key(source, task_id, release_id) -> str:
    return f"{source}-{task_id}-{release_id}"


def _path(key: str) -> Path:
    return REPORTS_DIR / f"{key}.jsonl"


def report_add(key: str, mode, src, ok, stem="", remark="", answer=""):
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": int(time.time()),
            "mode": int(mode) if isinstance(mode, int) else mode,
            "src": src or "?",
            "ok": 1 if ok else 0,
            "stem": str(stem)[:80],
            "remark": str(remark)[:80],
            "answer": str(answer)[:60],
        }
        with open(_path(key), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass


def report_load(key: str) -> list:
    p = _path(key)
    if not p.exists():
        return []
    try:
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out
    except OSError:
        return []


def report_stats(key: str) -> dict:
    recs = report_load(key)
    # 学习卡片(mode 0 / src=card)是"收集释义并跳过"，不参与判分统计，排除后正确率更真实
    judge = [r for r in recs if r.get("src") != "card"]
    total = len(judge)
    ok = sum(1 for r in judge if r.get("ok"))
    skip = sum(1 for r in judge if r.get("src") in ("skip", "blank"))
    src_dist = {}
    mode_dist = {}
    for r in recs:
        src = r.get("src", "?")
        src_dist[src] = src_dist.get(src, 0) + 1
        m = r.get("mode")
        mode_dist[str(m)] = mode_dist.get(str(m), 0) + 1
    return {
        "key": key,
        "total": total,
        "ok": ok,
        "fail": total - ok,
        "skip": skip,
        "accuracy": round(ok / total * 100, 1) if total else 0,
        "src_dist": src_dist,
        "mode_dist": mode_dist,
        "updated": recs[-1].get("ts") if recs else None,
    }


def list_reports() -> list:
    if not REPORTS_DIR.exists():
        return []
    out = []
    for p in sorted(REPORTS_DIR.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = report_stats(p.stem)
        st["file"] = p.name
        out.append(st)
    return out
