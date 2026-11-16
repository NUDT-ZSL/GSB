#!/usr/bin/env bash
# CDN edge-cache consistency workbench: repeatable demo (Linux/macOS/WSL).
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m cdnlab.demo
