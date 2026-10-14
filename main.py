#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cidaren-ultimate 统一入口
  python main.py               # Web 控制台（默认 http://localhost:5000）
  python main.py class 123 456 # CLI 刷单个班级任务
  python main.py study ...     # CLI 刷单个自学任务
  python main.py check         # 校验 token
"""
import sys

from cidaren.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
