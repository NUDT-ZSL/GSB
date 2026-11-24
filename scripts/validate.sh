#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python}"

"$PYTHON_BIN" generate_samples.py
"$PYTHON_BIN" -m pytest -q
"$PYTHON_BIN" scripts/replay_check.py \
  --data data \
  --out reports/order-independence.json

tmp_dir="$(mktemp -d)"
"$PYTHON_BIN" -m sensitivity_engine import \
  --data data \
  --db "$tmp_dir/ledger.sqlite3" \
  --report "$tmp_dir/import.json" \
  --checkpoint "$tmp_dir/checkpoint.json" \
  --concurrency 4 \
  --queue-limit 16
"$PYTHON_BIN" -m sensitivity_engine export-reentry \
  --db "$tmp_dir/ledger.sqlite3" \
  --out "$tmp_dir/reentry.json"
"$PYTHON_BIN" -m sensitivity_engine verify-checkpoint \
  --db "$tmp_dir/ledger.sqlite3" \
  --snapshot "$tmp_dir/checkpoint.json"
"$PYTHON_BIN" -m sensitivity_engine verify-truth \
  --db "$tmp_dir/ledger.sqlite3" \
  --truth truth/expected.json \
  --out "$tmp_dir/truth.json"

set +e
"$PYTHON_BIN" -m sensitivity_engine import \
  --data data/invalid \
  --db "$tmp_dir/invalid.sqlite3" \
  --report "$tmp_dir/invalid-import.json" \
  --checkpoint "$tmp_dir/invalid-checkpoint.json" \
  >/dev/null
invalid_code=$?
set -e
if [[ "$invalid_code" -ne 1 ]]; then
  echo "expected invalid sample exit code 1, got $invalid_code" >&2
  exit 1
fi

echo "validation passed; temporary artifacts in $tmp_dir"
