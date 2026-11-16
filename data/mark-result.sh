#!/usr/bin/env bash
set -euo pipefail
partition_key="${1:?partition key required}"
offset="${2:?offset required}"
result="${3:?result required}"
marker_out="${4:-output/consumer-markers.jsonl}"
mkdir -p "$(dirname "$marker_out")"
printf '{"ts":"%s","partition_key":"%s","offset":%s,"result":"%s"}\n' \
  "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$partition_key" "$offset" "$result" >> "$marker_out"
