#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv311/bin/python" ]]; then
  PYTHON="$ROOT/.venv311/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

"$PYTHON" -m unittest discover -s tests -t . -v

