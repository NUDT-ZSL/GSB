#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if [[ ! -x .venv/bin/python ]]; then
  "$PYTHON_BIN" -m venv --without-pip .venv
fi

find_wheel() {
  local pattern="$1"
  shift
  local dir
  for dir in "$@"; do
    [[ -d "$dir" ]] || continue
    find "$dir" -maxdepth 5 -type f -name "$pattern" 2>/dev/null | sort | head -1
  done
}

PIP_WHEEL="$(find_wheel 'pip-*.whl' /tmp/pylocal/usr/share/python-wheels /usr/share/python-wheels /tmp "$HOME/.cache/pip" /root/.cache/pip)"
SETUPTOOLS_WHEEL="$(find_wheel 'setuptools-*.whl' /tmp/pylocal/usr/share/python-wheels /usr/share/python-wheels /tmp "$HOME/.cache/pip" /root/.cache/pip)"
NUMPY_WHEEL="$(find_wheel 'numpy-*-cp311-*.whl' /tmp/wheelout /tmp "$HOME/.cache/pip" /root/.cache/pip)"

if [[ -z "$PIP_WHEEL" || -z "$SETUPTOOLS_WHEEL" ]]; then
  echo "offline pip/setuptools wheels not found" >&2
  exit 2
fi
if [[ -z "$NUMPY_WHEEL" ]]; then
  echo "cached NumPy cp311 wheel not found" >&2
  exit 2
fi

if ! .venv/bin/python -c 'import pip' >/dev/null 2>&1; then
  PYTHONPATH="$PIP_WHEEL" .venv/bin/python -m pip install --no-index \
    "$PIP_WHEEL" "$SETUPTOOLS_WHEEL"
fi

if ! .venv/bin/python -c 'import numpy' >/dev/null 2>&1; then
  .venv/bin/python -m pip install --no-index "$NUMPY_WHEEL"
fi

.venv/bin/python scripts/check_env.py
