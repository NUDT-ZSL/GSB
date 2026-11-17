#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -n "${PYTHON_BIN:-}" ]]; then
  PY="${PYTHON_BIN}"
elif command -v python3.11 >/dev/null 2>&1; then
  PY="python3.11"
elif command -v python3.10 >/dev/null 2>&1; then
  PY="python3.10"
else
  echo "Python 3.10 or newer is required" >&2
  exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
  "$PY" -m venv --without-pip .venv
fi

PIP_WHEEL="$ROOT/vendor/wheels/pip-22.0.2-py3-none-any.whl"
if ! .venv/bin/python -c 'import pip' >/dev/null 2>&1; then
  PYTHONPATH="$PIP_WHEEL" .venv/bin/python -m pip install \
    --no-index --find-links "$ROOT/vendor/wheels" pip==22.0.2
fi

.venv/bin/python -m pip install --no-index --find-links "$ROOT/vendor/wheels" \
  numpy==2.4.6 PyYAML==5.4.1

.venv/bin/python - <<'PY'
import numpy
import yaml

print(f"ready: numpy={numpy.__version__} PyYAML={yaml.__version__}")
PY
