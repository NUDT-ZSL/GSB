#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
"$PYTHON_BIN" -m venv .venv

if compgen -G "$ROOT/wheels/numpy*.whl" > /dev/null; then
  .venv/bin/python -m pip install --no-index --find-links "$ROOT/wheels" numpy
else
  .venv/bin/python -m pip install -r requirements.txt
fi
.venv/bin/python -m pip install -e . --no-deps

echo "Setup complete. Activate with: source .venv/bin/activate"

