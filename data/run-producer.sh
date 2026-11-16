#!/usr/bin/env bash
set -euo pipefail
address="${1:-127.0.0.1:19160}"
input_path="${2:-data/producer.jsonl}"
report="${3:-output/producer-report.json}"
exec ./output/bin/orderqueue produce --addr "$address" --input "$input_path" --report "$report"
