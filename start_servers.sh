#!/usr/bin/env bash
# 内部工单系统 - 附件预览服务启动脚本（Linux / macOS / Git Bash）
# 用法：./start_servers.sh
# 启动后：漏洞版 http://127.0.0.1:5000 ，修复版 http://127.0.0.1:5001
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p run logs reports

python preview_server/app.py --variant vulnerable --port 5000 >run/vulnerable.out.log 2>&1 &
echo "vulnerable pid=$! -> http://127.0.0.1:5000"
python preview_server/app.py --variant fixed --port 5001 >run/fixed.out.log 2>&1 &
echo "fixed      pid=$! -> http://127.0.0.1:5001"

echo "两个实例已在后台启动；停止请执行：kill \$(jobs -p) 或按端口结束进程。"
