#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="python"
fi

"$PYTHON" -m intent_interop generate --out-dir data
"$PYTHON" -m intent_interop baseline \
  --samples data/queries.jsonl \
  --v1 data/config_v1.txt \
  --v2 data/config_v2.txt \
  --v3 data/config_v3.json \
  --report reports/baseline.json
"$PYTHON" -m intent_interop demo --data-dir data --report-dir reports
"$PYTHON" -m unittest discover -s tests -v
