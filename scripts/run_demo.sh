#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x ".venv/bin/python" ]]; then
  uv venv --python 3.12 .venv
  uv pip install --python .venv/bin/python -e '.[test]'
fi

.venv/bin/python -m log_retention.sample_data --root sample
./retention --config sample/config.json load
./retention --config sample/config.json merge
./retention --config sample/config.json judge
./retention --config sample/config.json rebuild
./retention --config sample/config.json verify
./retention --config sample/config.json status

