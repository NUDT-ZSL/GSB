#!/usr/bin/env bash
# 一键演示：生成确定性样本 -> 剪辑 -> 展示产物与报告（macOS/Linux）
# 用法：bash run_demo.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

SAMPLE_DIR="$ROOT/samples"
OUT_DIR="$ROOT/output/demo"

rm -rf "$SAMPLE_DIR" "$OUT_DIR"

echo "== 1. 生成确定性样本 =="
python3 -X utf8 tools/generate_samples.py --dir "$SAMPLE_DIR"

echo
echo "== 2. 执行剪辑 =="
python3 -X utf8 -m wav_splitter \
  --wav "$SAMPLE_DIR/sample.wav" \
  --csv "$SAMPLE_DIR/marks.csv" \
  --output "$OUT_DIR"

echo
echo "== 3. 输出目录产物 =="
ls -l "$OUT_DIR"

echo
echo "== 4. JSON 报告 =="
cat "$OUT_DIR/report.json"

echo
echo "== 5. 运行验收测试 =="
python3 -m unittest discover -s tests -v
