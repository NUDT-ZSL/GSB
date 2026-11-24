#!/usr/bin/env bash
set -euo pipefail

WORK="$(mktemp -d /tmp/supply-chain-audit-XXXXXX)"
SAMPLE="$WORK/seed"
WORKSPACE="$WORK/checkpoint"

python generate_seed_samples.py "$SAMPLE"
python -m pytest -q
python -m supply_chain_audit import-snapshots \
  --sample-dir "$SAMPLE" \
  --workspace "$WORKSPACE" \
  --concurrency 4 \
  --timeout-seconds 10 \
  --queue-limit 32 \
  --max-hash-operations 10000 \
  --apply-seed-updates \
  --report "$WORK/import.json" >/dev/null
python -m supply_chain_audit query-view --workspace "$WORKSPACE" --status revoked --out "$WORK/query.json" >/dev/null
python -m supply_chain_audit export-closure --workspace "$WORKSPACE" --out "$WORK/closure.json" >/dev/null
python -m supply_chain_audit verify-checkpoint --workspace "$WORKSPACE"
echo "VALIDATION_ARTIFACTS=$WORK"
