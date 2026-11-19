#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing .venv Python. Follow README.md quick-start first." >&2
  exit 127
fi

exec "$PYTHON" "$ROOT/app.py" "$@"
