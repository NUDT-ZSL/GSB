#!/usr/bin/env bash
# 一键复现：建虚拟环境（如已存在则复用）、装依赖、生成样本、跑实验与测试。
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -d .venv ]; then
  if command -v uv >/dev/null 2>&1; then
    uv venv .venv
  else
    python3 -m venv .venv
  fi
fi

source .venv/bin/activate
python -m pip --version >/dev/null 2>&1 || uv pip install -r requirements.txt
python -c "import numpy" 2>/dev/null || uv pip install -r requirements.txt

python -m layout_order all || true
python -m unittest discover -s tests
