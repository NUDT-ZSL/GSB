#!/usr/bin/env bash
# Linux/macOS 一键演示：生成确定性样本并运行处理引擎
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p demo_workspace
python3 scripts/generate_samples.py --spec scripts/demo_spec.json --out demo_workspace
python3 -m mocap_audio.cli --session demo_workspace/session.json
