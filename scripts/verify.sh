#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

python3 -m compileall -q truth_ledger tests
python3 -m pytest
python3 -m truth_ledger.cli generate --out samples --cases 2
python3 -m truth_ledger.cli demo --root artifacts/demo
python3 -m truth_ledger.cli adjudicate \
  --case-dir artifacts/demo/normal-run/case-001 \
  --output artifacts/demo/normal-run/case-001/committed/reports/adjudication-standalone.json
python3 -m truth_ledger.cli self-check \
  --case-dir artifacts/demo/normal-run/case-001
