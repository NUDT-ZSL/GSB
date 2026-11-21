#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

for run in 1 2; do
  python -m scheduler_diag load \
    --bundle samples/main \
    --db "var/repeat-$run.sqlite" \
    --reset \
    --out "reports/repeat-$run-load.json" >/dev/null
  python -m scheduler_diag schedule \
    --bundle samples/main \
    --db "var/repeat-$run.sqlite" \
    --out "reports/repeat-$run-schedule.json" >/dev/null
  python -m scheduler_diag status \
    --bundle samples/main \
    --db "var/repeat-$run.sqlite" \
    --out "reports/repeat-$run-status.json" >/dev/null
done

cmp reports/repeat-1-load.json reports/repeat-2-load.json
cmp reports/repeat-1-schedule.json reports/repeat-2-schedule.json
cmp reports/repeat-1-status.json reports/repeat-2-status.json
echo "repeatability: byte-identical"
