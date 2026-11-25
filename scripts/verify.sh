#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python generate_samples.py
python -m proxy_diag verify --root .
python -m unittest -v tests.test_workbench
