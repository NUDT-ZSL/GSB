#!/usr/bin/env bash
# Deterministic end-to-end verification. Safe to re-run; outputs go under run/.
set -u

cd "$(dirname "$0")"
PYTHON="${PYTHON:-python3}"

echo "== regenerate deterministic samples =="
$PYTHON generate_samples.py

echo "== unit tests =="
$PYTHON -m unittest discover -s tests -p "test_pipeline.py" -v

echo "== baseline load + verify + status =="
$PYTHON -c "import shutil; shutil.rmtree('run/baseline', ignore_errors=True)"
$PYTHON -m datapipe --input-dir samples/baseline --work-dir run/baseline load
$PYTHON -m datapipe --input-dir samples/baseline --work-dir run/baseline \
  verify --report run/baseline-report.json
$PYTHON -m datapipe --input-dir samples/baseline --work-dir run/baseline status

echo "== invalidation =="
$PYTHON -c "import shutil; shutil.rmtree('run/invalidation', ignore_errors=True)"
$PYTHON -m datapipe --input-dir samples/invalidation --work-dir run/invalidation verify

echo "== fault code matrix =="
for scenario in fault_gen_digest fault_cache_rollback fault_cache_write \
                fault_prefetch fault_timeout fault_clock_rollback; do
  $PYTHON -c "import shutil; shutil.rmtree('run/$scenario', ignore_errors=True)"
  echo "-- $scenario"
  $PYTHON -m datapipe --input-dir "samples/$scenario" --work-dir "run/$scenario" schedule
done

echo "== budget exhaustion =="
$PYTHON -c "import shutil; shutil.rmtree('run/budget_exhausted', ignore_errors=True)"
$PYTHON -m datapipe --input-dir samples/budget_exhausted --work-dir run/budget_exhausted schedule

echo "== forced crash then resume =="
$PYTHON -c "import shutil; shutil.rmtree('run/fault_crash_generate', ignore_errors=True)"
$PYTHON -m datapipe --input-dir samples/fault_crash_generate \
  --work-dir run/fault_crash_generate schedule || true
$PYTHON -m datapipe --input-dir samples/fault_crash_generate \
  --work-dir run/fault_crash_generate resume

echo "== multi-process consistency =="
$PYTHON tests/test_multiprocess.py

echo "ALL CHECKS COMPLETED"
