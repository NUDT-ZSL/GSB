#!/usr/bin/env bash
# 一键演示（Git Bash / WSL / macOS / Linux）
# 用法: bash run_demo.sh
set -euo pipefail
cd "$(dirname "$0")"
PY=python3
command -v "$PY" >/dev/null 2>&1 || PY=python

echo "== 1. 生成确定性测试样本与清单 =="
"$PY" -I make_samples.py --workdir work

echo
echo "== 2. 运行主清单（含缺失/非PCM/位深/声道/跳过等分支）=="
"$PY" -I process.py \
  --manifest work/samples/edit_list.json \
  --output   work/output/dialogue_main.wav \
  --report   work/output/report_main.json \
  --log      work/output/run_main.log

echo
echo "== 3. 运行间隔清单（两个0.5s片段 + 200ms静音 = 1.2s）=="
"$PY" -I process.py \
  --manifest work/samples/gap_list.json \
  --output   work/output/dialogue_gap.wav \
  --report   work/output/report_gap.json \
  --log      work/output/run_gap.log

echo
echo "== 4. 运行 29 项自动化验收 =="
"$PY" -I verify.py
