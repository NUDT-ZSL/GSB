#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB="${DB:-state/acceptance.sqlite3}"
SAMPLES="${SAMPLES:-samples}"
ARTIFACT_DIR="${ARTIFACT_DIR:-artifacts/acceptance}"
mkdir -p "$ARTIFACT_DIR" state

python3 generate_samples.py --out "$SAMPLES"
python3 -m cache_ledger load --db "$DB" --samples "$SAMPLES"
python3 -m cache_ledger merge --db "$DB" --samples "$SAMPLES" \
  --report "$ARTIFACT_DIR/merge.json" > "$ARTIFACT_DIR/merge.stdout.json"
python3 -m cache_ledger admit --db "$DB" --samples "$SAMPLES" \
  --report "$ARTIFACT_DIR/admit.json" > "$ARTIFACT_DIR/admit.stdout.json"
python3 -m cache_ledger rebalance --db "$DB" --samples "$SAMPLES" \
  --report "$ARTIFACT_DIR/rebalance.json" > "$ARTIFACT_DIR/rebalance.stdout.json"
python3 -m cache_ledger evict --db "$DB" --samples "$SAMPLES" \
  --report "$ARTIFACT_DIR/evict.json" > "$ARTIFACT_DIR/evict.stdout.json"
python3 -m cache_ledger status --db "$DB" --samples "$SAMPLES" \
  --report "$ARTIFACT_DIR/status.json" > "$ARTIFACT_DIR/status.stdout.json"
python3 -m cache_ledger verify --db "$DB" --samples "$SAMPLES" \
  --report "$ARTIFACT_DIR/verify.json"

python3 -m unittest discover -s tests -b

echo "Acceptance completed: $DB"
