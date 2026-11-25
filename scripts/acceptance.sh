#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
ACC=".acceptance"

echo "[1/8] generate deterministic samples"
"$PYTHON" scripts/generate_samples.py --output samples

echo "[2/8] run automated tests"
"$PYTHON" -m pytest tests/test_layerforge.py

"$PYTHON" -c 'import shutil; shutil.rmtree(".acceptance", ignore_errors=True)'
mkdir -p "$ACC"

echo "[3/8] judge merged manifests and adaptive sampling"
"$PYTHON" -m layerforge judge \
  --workdir "$ACC/judge" \
  --report-dir "$ACC/judge/reports" \
  > "$ACC/judge.stdout.json"

echo "[4/8] one-shot build"
"$PYTHON" -m layerforge build \
  --workdir "$ACC/one" \
  --report-dir "$ACC/one/reports" \
  > "$ACC/one.build.json"

echo "[5/8] forced termination and byte-identical recovery"
set +e
"$PYTHON" -m layerforge build \
  --workdir "$ACC/crash" \
  --report-dir "$ACC/crash/reports" \
  --crash-step final-image \
  --crash-at object_renamed \
  > "$ACC/crash.before-exit.json"
crash_rc=$?
set -e
test "$crash_rc" -eq 88

"$PYTHON" -m layerforge resume \
  --workdir "$ACC/crash" \
  --report-dir "$ACC/crash/reports" \
  > "$ACC/crash.resume.json"
"$PYTHON" -m layerforge verify \
  --workdir "$ACC/crash" \
  --report-dir "$ACC/crash/reports" \
  --reference-dir "$ACC/one" \
  > "$ACC/crash.verify.json"

echo "[6/8] budget abandonment then resume"
set +e
"$PYTHON" -m layerforge build \
  --workdir "$ACC/budget" \
  --report-dir "$ACC/budget/reports" \
  --concurrency 2 \
  --round-budget 4 \
  > "$ACC/budget.build.json"
budget_rc=$?
set -e
test "$budget_rc" -eq 30

"$PYTHON" -m layerforge resume \
  --workdir "$ACC/budget" \
  --report-dir "$ACC/budget/reports" \
  --concurrency 2 \
  --round-budget 20 \
  > "$ACC/budget.resume.json"
"$PYTHON" -m layerforge verify \
  --workdir "$ACC/budget" \
  --report-dir "$ACC/budget/reports" \
  --reference-dir "$ACC/one" \
  > "$ACC/budget.verify.json"

echo "[7/8] parameter invalidation closure"
"$PYTHON" -m layerforge judge \
  --workdir "$ACC/param" \
  --report-dir "$ACC/param/reports" \
  --set-arg BUILD_CHANNEL=canary \
  > "$ACC/param.judge.json"

echo "[8/8] status and clock rollback diagnostic"
"$PYTHON" -m layerforge status \
  --workdir "$ACC/one" \
  --report-dir "$ACC/one/reports" \
  > "$ACC/status.json"
set +e
"$PYTHON" -m layerforge build \
  --workdir "$ACC/one" \
  --report-dir "$ACC/one/reports" \
  --clock-ms 1 \
  > "$ACC/clock-rollback.json"
clock_rc=$?
set -e
test "$clock_rc" -eq 25

echo "ACCEPTANCE_OK reports in $ACC/"
