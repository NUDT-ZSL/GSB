#!/usr/bin/env bash
# 游戏音效批处理工具一键演示（Git Bash / macOS / Linux）
# 用法：在项目根目录执行  bash demo.sh
set -euo pipefail

cd "$(dirname "$0")"

echo "=== 步骤 1/5：生成确定性测试样本（含有效/8bit/文本/损坏/超采样率 WAV）==="
python -I tests/make_fixtures.py --out testdata/input

echo
echo "=== 步骤 2/5：完整配置批处理（+6dB / -45dB 阈值 / 100ms 淡变）==="
python -I process_audio.py \
    --input testdata/input \
    --output testdata/output_full \
    --config configs/full.json \
    --report testdata/output_full/report.json

echo
echo "=== 步骤 3/5：空配置批处理（默认值，作为淡入系数核验的无淡变参考）==="
python -I process_audio.py \
    --input testdata/input \
    --output testdata/output_empty \
    --config configs/empty.json

echo
echo "=== 步骤 4/5：独立数值验收（对应任务验收 1/2/3/4/6/10）==="
python -I verify_outputs.py

echo
echo "=== 步骤 5/5：非法配置拦截演示（+40dB，预期退出码 2 且无输出）==="
set +e
python -I process_audio.py \
    --input testdata/input \
    --output testdata/output_should_not_exist \
    --config configs/bad_gain.json
code=$?
set -e
echo "非法配置退出码: $code（2 = 配置错误）"
if [ -e testdata/output_should_not_exist ]; then
    echo "警告：输出目录被意外创建"
else
    echo "输出目录是否被创建: False（符合预期）"
fi

echo
echo "演示完成。单元测试可执行： python -I run_tests.py"
