#!/usr/bin/env bash
# cidaren-ultimate Web 控制台启停脚本（容器内通用）
cd "$(dirname "$0")" || exit 1
export CIDAREN_HOST="${CIDAREN_HOST:-0.0.0.0}"
export CIDAREN_PORT="${CIDAREN_PORT:-5000}"
export CIDAREN_PASSWORD="${CIDAREN_PASSWORD:-cidaren2026}"
# /sdcard 是 FUSE，不支持 flock；数据目录放 ext4 以保住跨进程文件锁
export CIDAREN_DATA_DIR="${CIDAREN_DATA_DIR:-/root/cidaren-data}"
mkdir -p logs
if [ -f logs/web.pid ] && kill -0 "$(cat logs/web.pid)" 2>/dev/null; then
  echo "已在运行 pid=$(cat logs/web.pid)  http://127.0.0.1:$CIDAREN_PORT/?token=$CIDAREN_PASSWORD"
  exit 0
fi
setsid nohup python3 main.py > logs/web.log 2>&1 < /dev/null &
echo $! > logs/web.pid
sleep 3
echo "已启动 pid=$(cat logs/web.pid)"
echo "访问: http://127.0.0.1:$CIDAREN_PORT/?token=$CIDAREN_PASSWORD"
echo "日志: $(pwd)/logs/web.log"
