#!/usr/bin/env bash
set -euo pipefail

PYTHON="${PYTHON:-python3}"
"$PYTHON" generate_samples.py --root samples/generated
"$PYTHON" -m unittest discover -s tests
"$PYTHON" -m sbom_audit.verify --sample-dir samples/generated --work-dir .verify-work
"$PYTHON" run_audit.py import-snapshots \
  --sample-dir samples/generated \
  --fresh \
  --ledger .sbom-audit/ledger.sqlite3 \
  --checkpoint .sbom-audit/checkpoint.json \
  --report .sbom-audit/report.json
"$PYTHON" run_audit.py verify-checkpoint \
  --ledger .sbom-audit/ledger.sqlite3 \
  --checkpoint .sbom-audit/checkpoint.json
"$PYTHON" run_audit.py export-closure \
  --ledger .sbom-audit/ledger.sqlite3 \
  --checkpoint .sbom-audit/checkpoint.json \
  --out .sbom-audit/closure.json

echo "Verification completed."
