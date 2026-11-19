#!/usr/bin/env bash
# End-to-end deterministic demo: generate samples, run both regimes, verify.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY=.venv/bin/python

echo "== 1. Generate deterministic synthetic bundle =="
$PY -m voice_dialogue generate --out-dir data/samples

echo "== 2. Fine regime (small dialogs) =="
head -n 3 data/samples/manifest.jsonl > data/samples/manifest_small.jsonl
$PY -m voice_dialogue run \
  --manifest data/samples/manifest_small.jsonl \
  --config config.example.json \
  --report out/report_fine.json \
  --snapshot-dir .cache/snapshots

echo "== 3. Approximate regime (full mixed bundle) =="
$PY -m voice_dialogue run \
  --manifest data/samples/manifest.jsonl \
  --config config.example.json \
  --report out/report_approx.json

echo "== 4. Verify snapshot checksum/version =="
$PY -m voice_dialogue verify-snapshot --snapshot-dir .cache/snapshots

echo "== 5. Run unit tests =="
$PY -m unittest discover -s tests
