#!/usr/bin/env bash
# ============================================================
# 离线危险命令拦截规则审计工具 - POSIX 一键演示（Git Bash/Linux/macOS）
# 用法: bash run_demo.sh
# ============================================================
set -u
cd "$(dirname "$0")"

# 兼容 Windows Git Bash（python）与 Linux/macOS/WSL（python3）
if command -v python >/dev/null 2>&1; then
  PY=python
else
  PY=python3
fi
export PYTHONIOENCODING=utf-8

echo "==== 0. 运行 19 项验收单元测试 ===="
$PY -I tests/test_auditor.py || exit 1

echo
echo "==== 1. 检测阶段（audit）：识别历史拼接匹配绕过 ===="
$PY -I auditor.py --config samples/input/rules_valid.json \
  --commands samples/input/commands.txt --outdir samples/output/demo_audit || exit 1

echo
echo "==== 2. 修复阶段（remediated）：按规则语义重新判定 ===="
$PY -I auditor.py --config samples/input/rules_valid.json \
  --commands samples/input/commands.txt --outdir samples/output/demo_fixed --stage remediated || exit 1

echo
echo "==== 3. 异常配置：缺字段 / 坏 pattern / 重复 id ===="
$PY -I auditor.py \
  --config samples/input/rules_invalid_fields.json \
  --config samples/input/rules_bad_pattern.json \
  --config samples/input/rules_duplicate.json \
  --commands samples/input/commands.txt --outdir samples/output/demo_errors || exit 1

echo
echo "==== 4. 缺失输入（预期退出码 1，结果文件仍产出）===="
$PY -I auditor.py --config samples/input/not_exist.json \
  --config samples/input/rules_valid.json --commands samples/input/commands.txt \
  --outdir samples/output/demo_missing --retry 0
echo "（上一步退出码=1 为预期行为）"

echo
echo "==== 5. 无参数（预期打印用法并退出码 2）===="
$PY -I auditor.py
echo "（上一步退出码=2 为预期行为）"

echo
echo "==== 6. 瞬时不可读重试恢复演示 ===="
$PY -I examples/demo_retry.py || exit 1

echo
echo "全部演示完成，结果文件见 samples/output/"

