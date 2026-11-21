#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p artifacts

python3 -m unittest discover -v 2>&1 | tee artifacts/test_results.txt
python3 -m dimdiag generate
python3 -m dimdiag demo

printf '\nValidation artifacts:\n'
printf '  artifacts/demo_report.json\n'
printf '  artifacts/demo_summary.md\n'
printf '  artifacts/test_results.txt\n'

