#!/usr/bin/env bash
# 一键离线演示：生成样本 -> 完整运行 -> 运行测试
set -euo pipefail
cd "$(dirname "$0")"

python3 samples/generate_samples.py
python3 -m zh_nlp.cli \
  --config samples/config.json \
  --input samples/input.txt \
  --output out/demo
python3 -m unittest tests.test_pipeline
echo "演示输出目录: out/demo"
