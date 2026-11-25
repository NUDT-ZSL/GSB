#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 scripts/generate_samples.py
python3 -m pytest -q
python3 -m moe_router demo \
  --cases samples/cases.jsonl \
  --workdir artifacts/demo \
  --out artifacts/validation_report.json \
  --reset
