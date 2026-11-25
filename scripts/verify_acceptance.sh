#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
OUT="$ROOT/out"
mkdir -p "$OUT"

log() {
  printf '\n\033[1;36m== %s ==\033[0m\n' "$1"
}

run_expect_rc() {
  local expected="$1"
  shift
  set +e
  "$@"
  local actual=$?
  set -e
  if [[ "$actual" != "$expected" ]]; then
    echo "expected rc=$expected but got rc=$actual: $*" >&2
    exit 1
  fi
}

log "Generate deterministic samples"
"$PYTHON" -m capdiag generate --root "$ROOT"

log "Validate inputs"
"$PYTHON" -m capdiag validate

log "Run unit tests"
"$PYTHON" -m unittest discover -s tests

DB="$OUT/acceptance.sqlite"
TRUTH_DB="$OUT/acceptance-full-gen3.sqlite"
CONCURRENCY_DB="$OUT/acceptance-concurrent-same-service.sqlite"
THR_PARALLEL_DB="$OUT/acceptance-parallel-threshold.sqlite"
CRASH_DB="$OUT/acceptance-crash-recovery.sqlite"
THR_CRASH_DB="$OUT/acceptance-threshold-crash.sqlite"
rm -f "$DB" "$DB-wal" "$DB-shm" \
      "$TRUTH_DB" "$TRUTH_DB-wal" "$TRUTH_DB-shm" \
      "$CONCURRENCY_DB" "$CONCURRENCY_DB-wal" "$CONCURRENCY_DB-shm" \
      "$THR_PARALLEL_DB" "$THR_PARALLEL_DB-wal" "$THR_PARALLEL_DB-shm" \
      "$CRASH_DB" "$CRASH_DB-wal" "$CRASH_DB-shm" \
      "$THR_CRASH_DB" "$THR_CRASH_DB-wal" "$THR_CRASH_DB-shm"

log "Apply initial v1+v2 threshold view"
"$PYTHON" -m capdiag apply-threshold \
  --db "$DB" \
  --v1 samples/thresholds_v1.json \
  --v2-metadata samples/thresholds_v2_metadata.sqlite \
  --json "$OUT/acceptance-threshold-initial.json"

log "Run three service workers in parallel"
"$PYTHON" -m capdiag run --db "$DB" --service svc-alpha --report "$OUT/parallel-alpha.json" >"$OUT/parallel-alpha.log" 2>&1 &
P1=$!
"$PYTHON" -m capdiag run --db "$DB" --service svc-beta  --report "$OUT/parallel-beta.json"  >"$OUT/parallel-beta.log" 2>&1 &
P2=$!
"$PYTHON" -m capdiag run --db "$DB" --service svc-gamma --report "$OUT/parallel-gamma.json" >"$OUT/parallel-gamma.log" 2>&1 &
P3=$!
wait "$P1"
wait "$P2"
wait "$P3"
cat "$OUT/parallel-alpha.log" "$OUT/parallel-beta.log" "$OUT/parallel-gamma.log"
"$PYTHON" scripts/check_db.py --db "$DB" --expected-windows 54 --expected-metric-values 216 \
  --json "$OUT/acceptance-db-baseline.json"

log "Decimal high-precision verification"
"$PYTHON" -m capdiag verify --db "$DB" --json "$OUT/acceptance-verify-baseline.json"

log "Deterministic repeat verification"
"$PYTHON" -m capdiag verify --db "$DB" --json "$OUT/acceptance-verify-repeat.json" >/dev/null
"$PYTHON" - <<'PY'
import json
from pathlib import Path
a=json.loads(Path('out/acceptance-verify-baseline.json').read_text())
b=json.loads(Path('out/acceptance-verify-repeat.json').read_text())
for report in (a,b):
    report.pop('json', None)
assert a['checked_metric_values'] == b['checked_metric_values'] == 216
assert len(a['comparisons']) == len(b['comparisons'])
assert [(x['scope_id'],x['window_index'],x['metric'],x['fixed_value']) for x in a['comparisons']] == \
       [(x['scope_id'],x['window_index'],x['metric'],x['fixed_value']) for x in b['comparisons']]
print('repeat verification: bitwise fixed values and ordering match')
PY

log "Replace threshold snapshot and recompute invalid closure"
"$PYTHON" -m capdiag replace-threshold \
  --db "$DB" \
  --v1 samples/thresholds_v1.json \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite \
  --json "$OUT/acceptance-threshold-replacement.json"
"$PYTHON" -m capdiag verify --db "$DB" --json "$OUT/acceptance-verify-gen3.json"

log "Build independent full gen3 recomputation database"
"$PYTHON" -m capdiag apply-threshold \
  --db "$TRUTH_DB" \
  --v1 samples/thresholds_v1.json \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite \
  --json "$OUT/full-gen3-apply.json"
"$PYTHON" -m capdiag run --db "$TRUTH_DB" >"$OUT/full-gen3-run.log" 2>&1
"$PYTHON" scripts/check_db.py --db "$TRUTH_DB" --expected-windows 54 --expected-metric-values 216 \
  --json "$OUT/full-gen3-db-check.json"
THR_VERSION="$("$PYTHON" -c "import json; print(json.load(open('$OUT/acceptance-threshold-replacement.json'))['new_threshold_version'])")"
"$PYTHON" scripts/compare_evaluations.py \
  --incremental-db "$DB" \
  --truth-db "$TRUTH_DB" \
  --threshold-version "$THR_VERSION" \
  --json "$OUT/acceptance-eval-comparison.json"

log "Metric parameter invalidation plan"
"$PYTHON" -m capdiag plan-metric-change \
  --json "$OUT/acceptance-metric-change-plan.json"

log "Same-service concurrent workers commit once"
"$PYTHON" -m capdiag apply-threshold --db "$CONCURRENCY_DB" \
  --v2-metadata samples/thresholds_v2_metadata.sqlite >/dev/null
"$PYTHON" -m capdiag run --db "$CONCURRENCY_DB" --service svc-beta >"$OUT/concurrent-beta-1.log" 2>&1 &
C1=$!
"$PYTHON" -m capdiag run --db "$CONCURRENCY_DB" --service svc-beta >"$OUT/concurrent-beta-2.log" 2>&1 &
C2=$!
wait "$C1"
wait "$C2"
"$PYTHON" scripts/check_db.py --db "$CONCURRENCY_DB" --expected-windows 18 --expected-metric-values 72 \
  --json "$OUT/acceptance-concurrent-db.json"

log "Parallel threshold applicants replace one snapshot exactly once"
"$PYTHON" -m capdiag apply-threshold --db "$THR_PARALLEL_DB" \
  --v2-metadata samples/thresholds_v2_metadata.sqlite >/dev/null
"$PYTHON" -m capdiag apply-threshold --db "$THR_PARALLEL_DB" \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite \
  --json "$OUT/parallel-threshold-a.json" >"$OUT/parallel-threshold-a.log" 2>&1 &
T1=$!
"$PYTHON" -m capdiag apply-threshold --db "$THR_PARALLEL_DB" \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite \
  --json "$OUT/parallel-threshold-b.json" >"$OUT/parallel-threshold-b.log" 2>&1 &
T2=$!
wait "$T1"
wait "$T2"
"$PYTHON" - <<'PY'
import sqlite3
conn = sqlite3.connect('out/acceptance-parallel-threshold.sqlite')
assert conn.execute(
    "SELECT count(*) FROM threshold_snapshots WHERE snapshot_id='v2-sharded-gen3-alpha-cpu'"
).fetchone()[0] == 1
assert conn.execute(
    "SELECT count(*) FROM threshold_replacements "
    "WHERE snapshot_id='v2-sharded-gen3-alpha-cpu' AND status='committed'"
).fetchone()[0] == 1
assert conn.execute("SELECT generation FROM threshold_current WHERE id=1").fetchone()[0] == 3
print('parallel threshold replacement: exactly one committed snapshot')
PY

log "SIGKILL during window staging"
"$PYTHON" -m capdiag apply-threshold --db "$CRASH_DB" \
  --v2-metadata samples/thresholds_v2_metadata.sqlite >/dev/null
run_expect_rc 137 "$PYTHON" -m capdiag run --db "$CRASH_DB" --service svc-alpha \
  --crash-at stage --crash-window 2
"$PYTHON" -m capdiag recover --db "$CRASH_DB" --json "$OUT/acceptance-window-recovery.json"
"$PYTHON" -m capdiag run --db "$CRASH_DB" --service svc-alpha >"$OUT/crash-replay.log" 2>&1
"$PYTHON" scripts/check_db.py --db "$CRASH_DB" --expected-windows 18 --expected-metric-values 72 \
  --json "$OUT/acceptance-crash-db.json"

log "SIGKILL during threshold replacement"
"$PYTHON" -m capdiag apply-threshold --db "$THR_CRASH_DB" \
  --v2-metadata samples/thresholds_v2_metadata.sqlite >/dev/null
run_expect_rc 137 "$PYTHON" -m capdiag replace-threshold --db "$THR_CRASH_DB" \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite \
  --crash-at threshold-stage
"$PYTHON" -m capdiag recover --db "$THR_CRASH_DB" --json "$OUT/acceptance-threshold-recovery.json"
"$PYTHON" -m capdiag replace-threshold --db "$THR_CRASH_DB" \
  --v2-metadata samples/thresholds_v2_next_metadata.sqlite \
  --json "$OUT/acceptance-threshold-retry.json" >/dev/null

log "Threshold generation rollback must be rejected"
run_expect_rc 2 "$PYTHON" -m capdiag replace-threshold --db "$THR_CRASH_DB" \
  --v2-metadata samples/thresholds_v2_metadata.sqlite

log "Invalid trace sample must return business error code"
run_expect_rc 2 "$PYTHON" -m capdiag validate \
  --traces samples/invalid_traces.jsonl \
  --json "$OUT/acceptance-invalid-validation.json"
grep -q "E_TRACE_SEQUENCE" "$OUT/acceptance-invalid-validation.json"
grep -q "E_TRACE_CLOCK" "$OUT/acceptance-invalid-validation.json"

log "All acceptance checks passed"
