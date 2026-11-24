#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -d .venv ]]; then
  uv venv --python 3.11 .venv
  .venv/bin/python -m pip install -r requirements.txt
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pdre.cli generate-sample --out samples/demo

set +e
python -m pdre.cli run \
  --input-dir samples/demo \
  --report reports/acceptance-report.json \
  --checkpoint-dir state/acceptance/checkpoints \
  --snapshot-dir state/acceptance/snapshots \
  --ledger state/acceptance/ledger.jsonl \
  --batch-size 8
run_code=$?
set -e

# The fixed sample intentionally contains invalid records and business findings.
if [[ "$run_code" -ne 3 ]]; then
  echo "expected partial-failure exit code 3, got $run_code" >&2
  exit 1
fi

python -m pdre.cli verify \
  --input-dir samples/demo \
  --report reports/order-verification.json \
  --iterations 50

pytest -q
