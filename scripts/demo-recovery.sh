#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

scenario="$ROOT/output/demo-recovery"
reset_dir "$scenario"
trap 'stop_server_force' EXIT
start_server "$scenario" 19162 "$scenario/orderqueue.db" "$scenario/server-1.jsonl" "$scenario/recovery-1.json" "$scenario/ready-1.json" 30s

"$BIN" produce --addr 127.0.0.1:19162 --input data/producer.jsonl --report "$scenario/producer-report.json"
"$BIN" consume --addr 127.0.0.1:19162 --partition-key orders.eu --session recovery-session --limit 3 --window-timeout-ms 30000 --failure-plan data/no-failures.json --marker-out "$scenario/pull-only-markers.jsonl" --report "$scenario/pull-only.json" --pull-only
"$BIN" status --addr 127.0.0.1:19162 --out "$scenario/status-before-crash.json"

first_pid="$SERVER_PID"
stop_server_force "$first_pid"
start_server "$scenario" 19162 "$scenario/orderqueue.db" "$scenario/server-2.jsonl" "$scenario/recovery-2.json" "$scenario/ready-2.json" 30s

grep -q '"recovered_windows": \[' "$scenario/recovery-2.json"
"$BIN" consume --addr 127.0.0.1:19162 --partition-key orders.eu --session recovery-session-2 --limit 3 --window-timeout-ms 30000 --failure-plan data/no-failures.json --marker-out "$scenario/recovery-markers.jsonl" --report "$scenario/recovery-consumer.json" --target-offset 6
run_verify "$scenario/orderqueue.db" "$scenario/verification-report.json" "$scenario/verify.jsonl"
"$BIN" dump --db "$scenario/orderqueue.db" --out "$scenario/ledger-dump.json"
stop_server_force
trap - EXIT
echo "forced-termination recovery scenario passed: $scenario/recovery-2.json"
