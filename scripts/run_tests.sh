#!/usr/bin/env bash
# Reproduce the full unit-test suite inside the local virtual environment.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
if [ ! -x "$PY" ]; then
  echo "missing .venv; create it with: python -m venv .venv && pip install -r requirements.txt" >&2
  exit 1
fi
"$PY" -m unittest discover -s tests -v
