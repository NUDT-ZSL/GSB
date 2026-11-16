#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
python3 tools/demo.py
printf '\nOpen dashboard with:\npython3 tools/gui.py --data-dir .runtime/demo-data\n'
