#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3.11}"
fi

FIRST="$(mktemp)"
SECOND="$(mktemp)"
trap 'rm -f "$FIRST" "$SECOND"' EXIT
"$PYTHON" examples/library_demo.py >"$FIRST"
"$PYTHON" examples/library_demo.py >"$SECOND"
diff -u "$FIRST" "$SECOND"
echo "repeatability: OK ($(wc -c <"$FIRST") bytes identical)"
