#!/usr/bin/env bash
# 让绘本动起来 —— macOS / Linux 启动脚本
# 用法： ./start.sh [--folder 我的动画] [--port 8770] [--no-browser]
set -e
cd "$(dirname "$0")"

if command -v python3 >/dev/null 2>&1; then
  exec python3 app.py "$@"
elif command -v python >/dev/null 2>&1; then
  exec python app.py "$@"
else
  echo "找不到 python3。请先装 Python 3.8 以上。"
  exit 1
fi
