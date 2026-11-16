#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export LD_LIBRARY_PATH="$PWD/.runtime-libs:${LD_LIBRARY_PATH:-}"
exec .venv/bin/python -m pytest tests -q "$@"
