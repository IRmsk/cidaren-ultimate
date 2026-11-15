#!/bin/sh
cd "$(dirname "$0")" || exit 1
if [ -f logs/web.pid ] && kill "$(cat logs/web.pid)" 2>/dev/null; then
  rm -f logs/web.pid; echo "已停止"
else
  pkill -f "python3 main.py" && echo "已按进程名停止" || echo "未在运行"
fi
