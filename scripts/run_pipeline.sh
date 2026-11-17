#!/usr/bin/env bash
# 跨平台（Linux/macOS/Git-Bash）运行入口：生成样本并执行完整管线
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
python3 -m xling_pipeline.generate_data data/sample
python3 -m xling_pipeline.cli configs/pipeline_config.json
