# -*- coding: utf-8 -*-
"""
__main__.py — CLI 入口
  python -m cidaren                    # Web 控制台
  python -m cidaren class <task_id> <release_id>   # 刷单个班级任务
  python -m cidaren study <task_id> <list_id> <course_id> <task_type> <grade>
  python -m cidaren check              # 校验 token
  python -m cidaren resume             # 断点恢复：刷完所有未满分班级任务
"""
from __future__ import annotations

import sys

from .config import log


def main():
    args = sys.argv[1:]
    if not args:
        from .web import main as web_main
        web_main()
        return
    if args[0] == "check":
        from .config import get_missing_auth_fields, get_runtime_config
        from .login import is_token_valid
        cfg = get_runtime_config()
        missing = get_missing_auth_fields(cfg)
        if missing:
            raise SystemExit(f"缺少必要配置: {', '.join(missing)}")
        ok, info = is_token_valid(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))
        if ok:
            name = (info or {}).get("student_name") or (info or {}).get("nick_name") or "?"
            log(f"Token 有效: {name}")
        else:
            log("Token 无效或已过期")
        return
    if args[0] == "resume":
        """断点恢复：列出所有未满分班级任务并逐个刷（循环到满分）。"""
        from .api import Client, ApiError
        from .config import get_missing_auth_fields, get_runtime_config
        from .runner import run_full
        cfg = get_runtime_config()
        missing = get_missing_auth_fields(cfg)
        if missing:
            raise SystemExit(f"缺少必要配置: {', '.join(missing)}")
        client = Client(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))
        # 自动签到（默认关闭，需在 .env 设置 SIGNIN_ENABLED=1 开启）
        if str(cfg.get("SIGNIN_ENABLED", "0")) == "1":
            try:
                signin_resp = client.signin()
                signin_data = signin_resp.get("data") or {}
                if signin_data:
                    log(f"🏆 签到完成：累计 {signin_data.get('sign_in_total', '?')} 天，积分 +{signin_data.get('integral', '?')}")
            except Exception as e:
                log(f"签到失败（不影响刷题）: {e}")
        tasks, page = [], 1
        while page <= 20:
            resp = client.page_task(page=page, size=50)
            recs = (resp.get("data") or {}).get("records") or []
            if not recs:
                break
            tasks.extend(recs)
            if len(recs) < 50:
                break
            page += 1
        todo = [t for t in tasks if t.get("score", 0) < 100]
        if not todo:
            log("没有未完成的任务，全部已满分。")
            return
        log(f"待续任务 {len(todo)} 个，开始逐个刷满...")
        for i, t in enumerate(todo, 1):
            log(f"\n===== [{i}/{len(todo)}] {t.get('task_name')} =====")
            try:
                run_full(client, t.get("task_id"), t.get("release_id"), config=cfg,
                         task_type=t.get("task_type") or 1)
            except (ApiError, Exception) as e:
                log(f"任务异常: {e}")
        return
    if args[0] == "class":
        from .api import Client
        from .config import get_missing_auth_fields, get_runtime_config
        from .runner import run_full
        cfg = get_runtime_config()
        missing = get_missing_auth_fields(cfg)
        if missing:
            raise SystemExit(f"缺少必要配置: {', '.join(missing)}")
        task_id = int(args[1])
        release_id = int(args[2])
        task_type = int(args[3]) if len(args) >= 4 else 1
        client = Client(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))
        run_full(client, task_id, release_id, config=cfg, task_type=task_type)
        return
    if args[0] == "study":
        from .api import Client
        from .config import get_missing_auth_fields, get_runtime_config
        from .runner import run_study_full
        cfg = get_runtime_config()
        missing = get_missing_auth_fields(cfg)
        if missing:
            raise SystemExit(f"缺少必要配置: {', '.join(missing)}")
        task_id = int(args[1])
        list_id = args[2]
        course_id = args[3] if len(args) >= 4 else cfg.get("COURSE_ID", "CET4_v2")
        task_type = int(args[4]) if len(args) >= 5 else 3
        grade = int(args[5]) if len(args) >= 6 else int(cfg.get("STUDY_GRADE", "2") or 2)
        client = Client(cfg["USERTOKEN"], cfg.get("ABC", ""), cfg.get("AUTH_V", ""))
        run_study_full(client, task_id, course_id, list_id, task_type=task_type, grade=grade, config=cfg)
        return
    raise SystemExit(f"未知命令: {args[0]}")


if __name__ == "__main__":
    main()
