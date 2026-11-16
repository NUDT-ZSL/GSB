#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${OSWB_PORT:-9101}"
python3 -m oswb --host 127.0.0.1 --port "$PORT" --base-dir runtime/workbench-data --log-dir runtime/logs serve --echo
