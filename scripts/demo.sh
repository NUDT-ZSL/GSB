#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "missing .venv; run scripts/setup.sh first" >&2
  exit 2
fi

exec .venv/bin/python scripts/run_demo.py "$@"

