#!/usr/bin/env bash
# 项目 67 员工信息录入 —— 一键本地样例（Git Bash / WSL 风格，Windows 亦可运行）
# 用法：在项目根目录执行  bash run_sample.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PORT="${1:-8765}"
BASE_URL="http://127.0.0.1:${PORT}"
INPUT_CSV="$ROOT/samples/data/employees.csv"
OUT_DIR="$ROOT/samples/out"
REPORT="$OUT_DIR/sample_report.json"
EXPECTED="$ROOT/samples/expected/expected_summary.json"
mkdir -p "$OUT_DIR"

echo "==> [1/4] 启动本地模拟服务（端口 $PORT）..."
python -m mock_server.server --port "$PORT" >"$OUT_DIR/mock_server.log" 2>&1 &
SERVER_PID=$!
trap 'kill "$SERVER_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
    if curl -fsS "$BASE_URL/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 0.5
done
curl -fsS "$BASE_URL/healthz" >/dev/null
echo "    服务已就绪: $BASE_URL"

echo "==> [2/4] 运行填报工具..."
python -m employee_importer.cli \
    --input "$INPUT_CSV" \
    --report "$REPORT" \
    --url "$BASE_URL"

echo "==> [3/4] 报告与预期摘要对比..."
python tests/compare_sample.py "$REPORT" "$EXPECTED"

echo "==> [4/4] 完成。报告文件: $REPORT"
