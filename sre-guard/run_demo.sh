#!/usr/bin/env bash
# 跨平台演示入口（Windows 下可直接用等价的 python 命令，见 README）
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="src${PYTHONPATH:+:$PYTHONPATH}"
python3 samples/generate_samples.py --out data/events.ndjson
python3 -m sre_guard.cli --base-dir . "$@"
