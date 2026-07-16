#!/bin/bash
# 双击此文件：启动本地网页服务并打开浏览器。
# 停止服务：在弹出的终端窗口按 Ctrl+C，或直接关掉终端。
cd "$(dirname "$0")"
PYTHON="/Users/zhangzhenyu/.workbuddy/binaries/python/envs/default/bin/python"
echo "启动中… 浏览器将自动打开 http://localhost:8765"
# 后台启动服务
"$PYTHON" server.py &
SERVER_PID=$!
# 等2秒让服务起来
sleep 2
open "http://localhost:8765"
echo "✅ 网页已打开。关闭此窗口或按 Ctrl+C 停止服务。"
# 保持前台，Ctrl+C 退出时一并杀掉服务
trap "kill $SERVER_PID 2>/dev/null; exit" INT TERM
wait $SERVER_PID
