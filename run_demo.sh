#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
python3 -m llm_sampling_diag.cli demo --suite samples --out artifacts/demo
python3 -m unittest discover -s tests -v

