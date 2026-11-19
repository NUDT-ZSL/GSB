#!/usr/bin/env bash
# End-to-end deterministic demo + consistency report.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
"$PY" -m ocr_pipeline version
"$PY" -m ocr_pipeline demo --config config/demo_config.json --root "$PWD"
echo
echo "report: reports/consistency_report.json"
