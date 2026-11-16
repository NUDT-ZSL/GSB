#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

rm -rf .lab_state_demo
python3 -m regularization_lab run \
  --config configs/experiment.yaml \
  --state-dir .lab_state_demo \
  --report reports/demo_report.json \
  --commands commands/demo_commands.txt
