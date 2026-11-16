#!/usr/bin/env bash
set -euo pipefail
partition_key="${1:?partition key required}"
session="${2:?session required}"
address="${3:-127.0.0.1:19160}"
limit="${4:-10}"
window_timeout_ms="${5:-5000}"
failure_plan="${6:-data/no-failures.json}"
marker_out="${7:-output/consumer-markers.jsonl}"
report="${8:-output/consumer-report.json}"
target_offset="${9:-0}"
pull_only="${10:-false}"
args=(consume --addr "$address" --partition-key "$partition_key" --session "$session" --limit "$limit" --window-timeout-ms "$window_timeout_ms" --failure-plan "$failure_plan" --marker-out "$marker_out" --report "$report" --target-offset "$target_offset")
if [[ "$pull_only" == "true" ]]; then args+=(--pull-only); fi
exec ./output/bin/orderqueue "${args[@]}"
