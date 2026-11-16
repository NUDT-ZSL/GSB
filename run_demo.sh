#!/usr/bin/env bash
# Local deterministic inpainting/style-transfer editor demo (macOS/Linux).
# Requires Python 3.10+ with NumPy and Pillow installed; no network access.
set -euo pipefail
cd "$(dirname "$0")"
python3 scripts/generate_samples.py
python3 scripts/run_demo.py
