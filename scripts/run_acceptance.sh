#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m unittest discover -s tests -v
python3 -m regularization_lab verify \
  --config configs/experiment.yaml \
  --output reports/verification.json
