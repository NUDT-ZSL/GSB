#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
python generate_samples.py
python -m proxy_diag load --root .
python -m proxy_diag merge --root .
python -m proxy_diag replay --all --root .
python -m proxy_diag status --root .
python -m proxy_diag patch samples/patches/forward_table_patch_0001.json --root .
python -m proxy_diag replay --all --root .
python -m proxy_diag status --root .
