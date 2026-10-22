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
import os
import importlib.util

# 把当前目录加到 sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 直接加载 __main__.py（避免循环导入）
_spec = importlib.util.spec_from_file_location(
    "cidaren_main",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
)
_cidaren_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_cidaren_main)

if __name__ == "__main__":
    sys.exit(_cidaren_main.main())
