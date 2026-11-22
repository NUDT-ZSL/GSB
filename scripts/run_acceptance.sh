#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

"$PYTHON" -m unittest discover -s tests -v
"$PYTHON" -m sparse_inc.cli demo \
  --samples samples \
  --reports reports \
  --runs runs \
  --seed 20261123
"$PYTHON" scripts/recovery_demo.py \
  --manifest samples/cases.jsonl \
  --reports reports \
  --runs runs
