#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if command -v python3.11 >/dev/null 2>&1; then
  PYTHON=python3.11
else
  PYTHON=python3
fi

"$PYTHON" -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python scripts/generate_samples.py

echo "Virtual environment ready. Activate with: source .venv/bin/activate"
