#!/usr/bin/env bash
set -u

cd "$(dirname "$0")/.."

python -m auth_interop.samplegen --output samples --seed 20261125
python -m pytest -q

run_step() {
  echo
  echo "\$ $*"
  "$@"
  local code=$?
  echo "exit_code=${code}"
}

run_step python -m auth_interop.cli import \
  --sample-dir samples --state-dir state \
  --report artifacts/reports/import-report.json --force-rebuild

run_step python -m auth_interop.cli redeem \
  --state-dir state --sample-dir samples --clock samples/clock.json \
  --attempts samples/redemption_attempts_success.json \
  --report artifacts/reports/redeem-success.json

run_step python -m auth_interop.cli redeem \
  --state-dir state --sample-dir samples --clock samples/clock.json \
  --attempts samples/redemption_attempts.json \
  --report artifacts/reports/redeem-report.json

run_step python -m auth_interop.cli revoke \
  --state-dir state --sample-dir samples --clock samples/clock.json \
  --requests samples/revocation_requests.json \
  --report artifacts/reports/revoke-report.json

run_step python -m auth_interop.cli checkpoint \
  --state-dir state --report artifacts/reports/checkpoint-verify.json

run_step python -m auth_interop.cli import \
  --batch-dir samples/batch --state-root state/batch \
  --report-dir artifacts/reports/batch --force-rebuild

python -m auth_interop.cli view --state-dir state \
  --revocations samples/revocation_requests.json --format table
