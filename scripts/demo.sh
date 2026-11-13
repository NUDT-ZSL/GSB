#!/usr/bin/env bash
# 本机反射型脚本注入探测工具 —— 一键演示（Git Bash，仅标准库）。
# 全程只访问 127.0.0.1 回环地址，不访问任何外部目标。
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VULN_PORT="${VULN_PORT:-8033}"
FIXED_PORT="${FIXED_PORT:-8034}"

wait_ready() {
  local url="$1"
  for _ in $(seq 1 30); do
    python -c "import urllib.request,sys;urllib.request.urlopen('$url',timeout=1).read()" 2>/dev/null && return 0
    sleep 0.3
  done
  echo "服务未就绪: $url" >&2
  exit 1
}

python demo_site/server.py --port "$VULN_PORT" --mode vulnerable &
VULN_PID=$!
python demo_site/server.py --port "$FIXED_PORT" --mode fixed &
FIXED_PID=$!
trap 'kill $VULN_PID $FIXED_PID 2>/dev/null' EXIT

wait_ready "http://127.0.0.1:$VULN_PORT/safe?q=ready"
wait_ready "http://127.0.0.1:$FIXED_PORT/safe?q=ready"
mkdir -p samples reports

echo; echo "[1/5] 生成确定性样本"
python -m xss_probe gen-cases  --output samples/cases.json
python -m xss_probe gen-history --output samples/history_report.json

echo; echo "[2/5] 脆弱版站点扫描（预期退出码 1）"
python -m xss_probe scan --base-url "http://127.0.0.1:$VULN_PORT" \
  --cases samples/cases.json --report reports/vuln_run1.json \
  --fix-status "vulnerable-2026-11-14" --history samples/history_report.json
echo "脆弱版退出码 = $?（预期 1）"

echo; echo "[3/5] 重复扫描验证稳定性"
python -m xss_probe scan --base-url "http://127.0.0.1:$VULN_PORT" \
  --cases samples/cases.json --report reports/vuln_run2.json \
  --fix-status "vulnerable-2026-11-14" --history samples/history_report.json >/dev/null
python -c "import json;a=json.load(open('reports/vuln_run1.json',encoding='utf-8'));b=json.load(open('reports/vuln_run2.json',encoding='utf-8'));print('两次报告完全一致:', json.dumps(a,ensure_ascii=False,sort_keys=True)==json.dumps(b,ensure_ascii=False,sort_keys=True))"

echo; echo "[4/5] 修复版站点复核（预期退出码 0）"
python -m xss_probe scan --base-url "http://127.0.0.1:$FIXED_PORT" \
  --cases samples/cases.json --report reports/fixed_run.json \
  --fix-status "fixed-2026-11-14" --history samples/history_report.json
echo "修复版退出码 = $?（预期 0）"

echo; echo "[5/5] 外部地址拒绝演示（预期退出码 3）"
python -m xss_probe scan --base-url "http://example.com/" \
  --cases samples/cases.json --report reports/blocked.json \
  --fix-status blocked-test || true
