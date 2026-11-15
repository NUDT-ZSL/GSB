#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run: uv venv --python 3.11 .venv && .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi
if [[ "$#" -eq 0 ]]; then
  exec .venv/bin/python -m tactical_engine run
fi
exec .venv/bin/python -m tactical_engine "$@"
