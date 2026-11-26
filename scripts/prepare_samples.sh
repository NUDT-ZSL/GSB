#!/usr/bin/env bash
# 从“工程交付目录” engineering_delivery/ 复制固定种子脚本到工作区并生成样本。
# 用法: bash scripts/prepare_samples.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[prepare] 复制固定种子脚本到工作区根目录"
cp engineering_delivery/seed_samples.py ./seed_samples.py

echo "[prepare] 确定性生成本地样本 (samples/)"
python3 seed_samples.py --out samples --force

echo "[prepare] 完成。SQLite 元数据库: samples/policy_v2/meta.sqlite"
