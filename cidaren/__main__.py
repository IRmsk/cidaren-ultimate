# -*- coding: utf-8 -*-
"""
__main__.py — CLI 入口
  python -m cidaren                    # Web 控制台
  python -m cidaren class <task_id> <release_id>   # 刷单个班级任务
  python -m cidaren study <task_id> <list_id> <course_id> <task_type> <grade>
  python -m cidaren check              # 校验 token
"""
from __future__ import annotations

import sys


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
            print(f"Token 有效: {name}")
        else:
            print("Token 无效或已过期")
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
