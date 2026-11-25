#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

python3 -m opverify.cli generate --out data/seed-sample
python3 -m opverify.cli validate-sample --sample data/seed-sample
python3 -m unittest discover -s tests -v
python3 -m opverify.cli verify --workspace runs/verification

echo "Verification report: $ROOT_DIR/artifacts/opverify_verification_report.json"
