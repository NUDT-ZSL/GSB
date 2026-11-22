#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 10):
    raise SystemExit("Python 3.10 or newer is required")
print(f"Python {sys.version.split()[0]}")
PY

if [ ! -x .venv/bin/python ]; then
  if command -v virtualenv >/dev/null 2>&1; then
    virtualenv -p "$PYTHON_BIN" .venv
  else
    "$PYTHON_BIN" -m venv .venv
  fi
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e .
echo "Environment ready: $(.venv/bin/python --version), NumPy $(.venv/bin/python -c 'import numpy; print(numpy.__version__)')"
