#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 scripts/generate_samples.py
python3 -m drift_monitor --runtime runtime demo \
  --config samples/monitor_v3.json \
  --stream samples/data/batch_stream.json --clean
