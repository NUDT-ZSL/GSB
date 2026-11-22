#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
STATE="$ROOT/state/demo"
mkdir -p "$STATE"

"$PY" - <<'PY'
from pathlib import Path
for path in Path("state/demo").glob("*"):
    if path.is_file():
        path.unlink()
PY

"$PY" tools/generate_samples.py
"$PY" -m fieldengine run --experiment samples/basic/experiment.json --db "$STATE/basic.sqlite3"
"$PY" -m fieldengine verify --experiment samples/basic/experiment.json --db "$STATE/basic.sqlite3"
"$PY" -m fieldengine status --db "$STATE/basic.sqlite3"

set +e
"$PY" -m fieldengine run --experiment samples/budget/experiment.json --db "$STATE/budget.sqlite3"
BUDGET_EXIT=$?
set -e
echo "budget_run_exit=$BUDGET_EXIT (expected 4)"

"$PY" - <<'PY'
from pathlib import Path
for path in Path("state/demo").glob("crash.sqlite3*"):
    path.unlink()
PY
set +e
"$PY" -m fieldengine run --experiment samples/crash_running/experiment.json --db "$STATE/crash.sqlite3"
CRASH_EXIT=$?
set -e
echo "crash_run_exit=$CRASH_EXIT (expected 137)"
"$PY" -m fieldengine status --db "$STATE/crash.sqlite3"
"$PY" -m fieldengine resume --experiment samples/crash_running/experiment.json --db "$STATE/crash.sqlite3"
"$PY" -m fieldengine verify --experiment samples/crash_running/experiment.json --db "$STATE/crash.sqlite3"
