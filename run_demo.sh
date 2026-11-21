#!/usr/bin/env bash
# Reproducible end-to-end demo: deterministic samples, parallel rerun equality,
# budget/memory arbitration and snapshot rollback/corruption handling.
# Purely local; no network access or external services required.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

OUT="${RF_DEMO_OUTPUT:-artifacts/demo}"
mkdir -p "$OUT"

echo "==> [1/5] unit tests"
python -m unittest tests.test_pipeline -v

echo "==> [2/5] run with 1 worker"
python -m recall_fusion.cli run --output "$OUT/run_w1"

echo "==> [3/5] rerun with 3 workers and different batch size"
RF_WORKERS=3 RF_BATCH_SIZE=6 python -m recall_fusion.cli run --output "$OUT/run_w3"

echo "==> [4/5] byte-level reproducibility check"
python -m recall_fusion.cli verify "$OUT/run_w1" "$OUT/run_w3"

echo "==> [5/5] snapshot versioning demo (rollback + corruption)"
python -m recall_fusion.cli snapshot-demo --output "$OUT"

echo "Demo artifacts written under $OUT"
