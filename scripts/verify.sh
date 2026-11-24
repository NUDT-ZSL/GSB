#!/usr/bin/env bash
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON:-python}"
STATE_DIR="${STATE_DIR:-state-demo}"

echo "[1/6] Python"
"$PYTHON_BIN" --version

echo "[2/6] Dependencies"
"$PYTHON_BIN" -m pip install -r requirements-dev.txt

echo "[3/6] Generate samples and truth"
"$PYTHON_BIN" scripts/generate_samples.py

echo "[4/6] Pytest"
"$PYTHON_BIN" -m pytest -q

echo "[5/6] Determinism marker"
"$PYTHON_BIN" scripts/verify_determinism.py

echo "[6/6] CLI workflow"
"$PYTHON_BIN" -c 'import shutil, pathlib; shutil.rmtree(pathlib.Path("state-demo"), ignore_errors=True)'
mkdir -p "$STATE_DIR"
run_expected_partial() {
  set +e
  "$PYTHON_BIN" -m offline_authz "$@"
  local code=$?
  set -e
  if [[ "$code" -ne 1 ]]; then
    echo "expected partial exit code 1, got $code: $*" >&2
    exit 1
  fi
}
set -e
run_expected_partial import --input-dir samples/generated --state-dir "$STATE_DIR"
run_expected_partial adjudicate --input-dir samples/generated --state-dir "$STATE_DIR" --watermark 1
run_expected_partial adjudicate --input-dir samples/generated --state-dir "$STATE_DIR" --watermark 2
run_expected_partial revoke --input-dir samples/generated --state-dir "$STATE_DIR" --watermark 2
"$PYTHON_BIN" -m offline_authz report --state-dir "$STATE_DIR" --output "$STATE_DIR/diagnostic-report.json"
"$PYTHON_BIN" -m offline_authz verify-checkpoint --state-dir "$STATE_DIR"

echo "Verification completed. Partial exit code 1 is intentional for seeded invalid/denied records."
