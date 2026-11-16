#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

scenario="$ROOT/output/demo-normal"
reset_dir "$scenario"
trap 'stop_server_force' EXIT
start_server "$scenario" 19160 "$scenario/orderqueue.db" "$scenario/server.jsonl" "$scenario/recovery-report.json" "$scenario/ready.json" 5s

"$BIN" produce --addr 127.0.0.1:19160 --input data/producer.jsonl --report "$scenario/producer-report.json"

"$BIN" consume --addr 127.0.0.1:19160 --partition-key orders.eu --session normal-eu --limit 2 --window-timeout-ms 5000 --failure-plan data/no-failures.json --marker-out "$scenario/eu-markers.jsonl" --report "$scenario/eu-consumer.json" --target-offset 6 > "$scenario/eu-consumer.stdout" &
eu_pid=$!
"$BIN" consume --addr 127.0.0.1:19160 --partition-key orders.us --session normal-us --limit 2 --window-timeout-ms 5000 --failure-plan data/no-failures.json --marker-out "$scenario/us-markers.jsonl" --report "$scenario/us-consumer.json" --target-offset 4 > "$scenario/us-consumer.stdout" &
us_pid=$!
"$BIN" consume --addr 127.0.0.1:19160 --partition-key audit.internal --session normal-audit --limit 3 --window-timeout-ms 5000 --failure-plan data/no-failures.json --marker-out "$scenario/audit-markers.jsonl" --report "$scenario/audit-consumer.json" --target-offset 3 > "$scenario/audit-consumer.stdout" &
audit_pid=$!
wait "$eu_pid"
wait "$us_pid"
wait "$audit_pid"

run_verify "$scenario/orderqueue.db" "$scenario/verification-report.json" "$scenario/verify.jsonl"
"$BIN" dump --db "$scenario/orderqueue.db" --out "$scenario/ledger-dump.json"
stop_server_force
trap - EXIT
echo "normal scenario passed: $scenario/verification-report.json"
