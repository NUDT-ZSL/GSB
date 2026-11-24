#!/usr/bin/env bash
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON:-python3}"

run_expect() {
  local expected="$1"
  shift
  local code
  "$PYTHON_BIN" "$@"
  code=$?
  if [[ "$code" -ne "$expected" ]]; then
    echo "EXPECTED_EXIT=$expected ACTUAL_EXIT=$code COMMAND=$*" >&2
    exit 1
  fi
  echo "OK expected_exit=$expected actual_exit=$code"
}

echo '## 1. Generate fixed-seed samples'
run_expect 0 -m screen_diag generate --out samples

echo '## 2. Unit tests'
run_expect 0 -m unittest discover -s tests -v

echo '## 3. Baseline adoption/merge/replay'
run_expect 0 -m screen_diag analyze \
  --samples samples \
  --report reports/baseline.json \
  --state-dir state/validate-baseline \
  --workers 4 \
  --memory-budget-bytes 65536

echo '## 4. Digest corruption injection -> E_DIGEST/3'
run_expect 3 -m screen_diag analyze \
  --samples samples --inject corrupt_digest \
  --inject-workdir out/validation-injected \
  --report reports/validate-corrupt-digest.json \
  --state-dir state/validate-corrupt-digest \
  --workers 2 --memory-budget-bytes 4096

echo '## 5. Screen count mismatch -> E_DIGEST/3'
run_expect 3 -m screen_diag analyze \
  --samples samples --inject screen_count_mismatch \
  --inject-workdir out/validation-injected \
  --report reports/validate-count-mismatch.json \
  --state-dir state/validate-count-mismatch

echo '## 6. Out-of-bounds logical time -> E_SCHEMA/2'
run_expect 2 -m screen_diag analyze \
  --samples samples --inject time_out_of_bounds \
  --inject-workdir out/validation-injected \
  --report reports/validate-oob.json

echo '## 7. Replay drift -> E_REPLAY/4'
run_expect 4 -m screen_diag analyze \
  --samples samples --inject replay_drift \
  --inject-workdir out/validation-injected \
  --report reports/validate-replay-drift.json \
  --state-dir state/validate-replay-drift

echo '## 8. Interrupt after two chunks -> 130, then resume -> 0'
run_expect 130 -m screen_diag analyze \
  --samples samples \
  --state-dir state/validate-interrupt \
  --report reports/validate-interrupted.json \
  --interrupt-after-chunks 2
run_expect 0 -m screen_diag analyze \
  --samples samples \
  --state-dir state/validate-interrupt \
  --report reports/validate-resumed.json \
  --resume
run_expect 0 -m screen_diag recover --samples samples --state-dir state/validate-interrupt

echo 'VALIDATION_OK'
