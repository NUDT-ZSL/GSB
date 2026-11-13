#!/usr/bin/env bash
# 本地订单查询接口 SQL 注入自查 —— 一键可复现演示（POSIX shell：git-bash / WSL / macOS / Linux）
# 用法： bash demo.sh
# 纯标准库、仅回环、确定性样本；无需公网、无真实凭据、不使用 Docker。
set -u
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT"
PY="${PYTHON:-python3}"
WS="$ROOT/lab_ws"
PIDS=()

wait_port() {
  local port="$1"
  "$PY" - "$port" <<'EOF'
import socket, sys, time
port = int(sys.argv[1]); deadline = time.time() + 10
while time.time() < deadline:
    s = socket.socket()
    try:
        s.connect(("127.0.0.1", port)); s.close(); sys.exit(0)
    except OSError:
        s.close(); time.sleep(0.2)
sys.exit(f"port {port} not ready")
EOF
}

start_server() {
  local mode="$1" port="$2" fail_after="${3:-}"
  if [ -n "$fail_after" ]; then export SQLI_AUDIT_FAIL_AFTER="$fail_after"; else unset SQLI_AUDIT_FAIL_AFTER; fi
  "$PY" "lab_ws/server_${mode}.py" >/dev/null 2>&1 &
  PIDS+=($!)
  wait_port "$port"
  echo "  [启动] mode=$mode port=$port pid=${PIDS[-1]}"
}

cleanup() {
  unset SQLI_AUDIT_FAIL_AFTER
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT

section() { printf '\n========== %s ==========\n' "$1"; }

section "0. 重新生成确定性实验环境"
rm -rf "$WS/reports" "$WS/state"
"$PY" -m sqli_audit setup "$WS" >/dev/null
echo "  实验环境: $WS"

section "14. 内置自检（期望 42/42 通过，退出码 0）"
"$PY" -m sqli_audit selfcheck || { echo "自检失败"; exit 1; }

section "1. 缺字段配置（期望退出码 1，列出缺失字段，无请求发出）"
printf '{"base_url":"http://127.0.0.1:8031","endpoint_path":"/orders","method":"GET","baseline":{"order_no":"ORD-1001"}}' \
  > "$WS/bad_missing.json"
"$PY" -m sqli_audit scan "$WS/bad_missing.json" || true
echo "  上一条命令退出码=$?（期望 1）"

section "2. 服务未启动时扫描（期望可达性失败，退出码 2）"
"$PY" -m sqli_audit scan "$WS/target_vulnerable.json" || true
echo "  上一条命令退出码=$?（期望 2）"

section "3/5/6. 缺陷接口扫描（可疑定位 + 已确认 + 证据 + 字节复现）"
start_server vulnerable 8031
"$PY" -m sqli_audit scan "$WS/target_vulnerable.json"
cp "$WS/reports/target_vulnerable_report.json" "$WS/reports/run1.json"
"$PY" -m sqli_audit scan "$WS/target_vulnerable.json" >/dev/null
cp "$WS/reports/target_vulnerable_report.json" "$WS/reports/run2.json"
[ "$(sha256sum "$WS/reports/run1.json" | cut -d' ' -f1)" = \
  "$(sha256sum "$WS/reports/run2.json" | cut -d' ' -f1)" ] && echo "  [验收6] 两次报告逐字节一致: True"

section "4. 纯回显正常接口（期望零可疑）"
start_server echo 8034
"$PY" -m sqli_audit scan "$WS/target_echo.json"

section "7. 参数化修复版本对照（期望全部 fixed）"
start_server fixed 8032
"$PY" -m sqli_audit compare "$WS/target_fixed.json" \
  --before "$WS/reports/target_vulnerable_report.json" \
  --output "$WS/reports/comparison_fixed.json"

section "8. 部分修复版本（期望 order_no 残留单列）"
start_server partial 8033
"$PY" -m sqli_audit compare "$WS/target_partial.json" \
  --before "$WS/reports/target_vulnerable_report.json" \
  --output "$WS/reports/comparison_partial.json"

section "9/11. 故障门控（期望不中断 + 未完成项 + 覆盖不全提示）"
start_server flaky 8035 5
"$PY" -m sqli_audit scan "$WS/target_flaky.json" --state "$WS/state/demo_flaky_state.json"

section "10. 恢复服务后同状态文件重跑（期望补齐、不重复计数）"
cleanup; PIDS=()
start_server flaky 8035
"$PY" -m sqli_audit scan "$WS/target_flaky.json" --state "$WS/state/demo_flaky_state.json"

section "12/13. 时间占位与工作目录无关性"
"$PY" -c "import json;r=json.load(open(r'$WS/reports/target_flaky_report.json',encoding='utf-8'));print('  generated_at =',r['generated_at'],'| elapsed =',r['elapsed'])"
mkdir -p "$WS/cwd_a" "$WS/cwd_b"
(cd "$WS/cwd_a" && "$PY" -m sqli_audit scan "$WS/target_vulnerable.json" --report "$WS/reports/cwd_a.json" --state "$WS/state/cwd_a.json" >/dev/null)
(cd "$WS/cwd_b" && "$PY" -m sqli_audit scan "$WS/target_vulnerable.json" --report "$WS/reports/cwd_b.json" --state "$WS/state/cwd_b.json" >/dev/null)
[ "$(sha256sum "$WS/reports/cwd_a.json" | cut -d' ' -f1)" = \
  "$(sha256sum "$WS/reports/cwd_b.json" | cut -d' ' -f1)" ] && echo "  [验收13] 不同工作目录报告一致: True"

echo
echo "演示完成。报告目录: $WS/reports"
