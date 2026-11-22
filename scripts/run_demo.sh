#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p runs
.venv/bin/python -m prefworkbench generate \
  --out data/sample.jsonl \
  --config configs/default.json \
  --count 120 \
  --seed 20261123
.venv/bin/python -m prefworkbench run \
  --input data/sample.jsonl \
  --config configs/default.json \
  --out runs/demo \
  --cache cache/demo > runs/demo_summary.json
.venv/bin/python -m prefworkbench verify --run runs/demo
