#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB="${1:-var/acceptance.sqlite3}"
python3 scripts/generate_samples.py --out samples >/dev/null
python3 -m unittest discover -s tests -v
python3 -m engine_diag --db "$DB" load \
  --tasks samples/tasks_v1.jsonl \
  --stats samples/platform_stats.json \
  --plan samples/plans_v1.json >/dev/null
python3 -m engine_diag --db "$DB" build --plan-version p1 >/dev/null
python3 -m engine_diag --db "$DB" verify --plan-version p1

