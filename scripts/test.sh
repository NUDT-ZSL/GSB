#!/usr/bin/env sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"
go test ./...
python3 -m unittest discover -s tests -v
python3 tools/demo.py >/tmp/message-interop-demo.json
python3 tools/gui.py --data-dir .runtime/demo-data --check >/tmp/message-interop-gui-check.json
python3 tools/verify.py --data-dir .runtime/demo-data >/tmp/message-interop-verify.json
echo "Demo verification passed; full output: /tmp/message-interop-demo.json"
