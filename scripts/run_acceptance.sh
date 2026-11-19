#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
if [[ -x .venv/bin/python ]]; then
  source .venv/bin/activate
fi

export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
python -m pytest -q
pref-workbench demo --workspace .
