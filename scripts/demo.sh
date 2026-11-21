#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DB="var/demo.sqlite"

python -m scheduler_diag load \
  --bundle samples/main \
  --db "$DB" \
  --reset \
  --out reports/demo-load.json

python -m scheduler_diag schedule \
  --bundle samples/main \
  --db "$DB" \
  --out reports/demo-schedule.json

python -m scheduler_diag status \
  --bundle samples/main \
  --db "$DB" \
  --out reports/demo-status.json

python -m scheduler_diag inject \
  --bundle samples/main \
  --db "$DB" \
  --out reports/demo-injections.json

python -m scheduler_diag verify \
  --bundle samples/main \
  --db "$DB" \
  --out reports/demo-verify.json
