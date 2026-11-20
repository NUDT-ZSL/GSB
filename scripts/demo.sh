#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Python 3.11 venv not found at $PYTHON" >&2
  exit 2
fi

"$PYTHON" - <<'PY'
import sqlite3
import sys
import numpy

if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11+ is required")
print(
    f"python={sys.version.split()[0]} "
    f"sqlite={sqlite3.sqlite_version} numpy={numpy.__version__}"
)
PY

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

SMALL_DB="$TMP/small.db"
SCALE_DB="$TMP/scale.db"
CRASH_DB="$TMP/crash.db"

"$PYTHON" -m kdrift load --sample samples --db "$SMALL_DB" \
  >"$TMP/load-small.json"
"$PYTHON" -m kdrift diff --db "$SMALL_DB" >"$TMP/diff-a.json"
"$PYTHON" -m kdrift diff --db "$SMALL_DB" >"$TMP/diff-b.json"
cmp -s "$TMP/diff-a.json" "$TMP/diff-b.json"
"$PYTHON" - "$TMP/diff-a.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["algorithm"]["mode"] == "exact"
assert data["summary"]["drift_count"] == 10
assert data["summary"]["by_type"] == {
    "extra": 3,
    "field_mismatch": 1,
    "missing": 4,
    "replicas_mismatch": 1,
    "selector_mismatch": 1,
}
print("small-diff: exact, 10 drifts, byte-stable")
PY

"$PYTHON" -m kdrift plan --db "$SMALL_DB" >"$TMP/plan.json"
"$PYTHON" - "$TMP/plan.json" <<'PY'
import json
import sys

plan = json.load(open(sys.argv[1], encoding="utf-8"))
assert plan["valid"]
assert plan["summary"]["by_action"] == {
    "create": 4,
    "delete": 3,
    "noop": 1,
    "update": 2,
}
assert plan["constraints"]["mutual_exclusion_pairs"]
print(
    "small-plan: valid, "
    f"{plan['summary']['operation_count']} operations, "
    f"{plan['summary']['dependency_count']} dependencies"
)
PY

"$PYTHON" -m kdrift apply --db "$SMALL_DB" >"$TMP/apply.json"
"$PYTHON" - "$TMP/apply.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["converged"]
assert data["verification"]["ok"]
print(
    f"small-apply: converged in {data['round_count']} rounds, "
    f"logical_clock={data['logical_clock_ms']}, verify=ok"
)
PY

"$PYTHON" -m kdrift load --sample samples/scale --db "$SCALE_DB" \
  >"$TMP/load-scale.json"
"$PYTHON" -m kdrift diff --db "$SCALE_DB" >"$TMP/diff-scale.json"
"$PYTHON" -m kdrift diff --db "$SCALE_DB" >"$TMP/diff-scale-b.json"
cmp -s "$TMP/diff-scale.json" "$TMP/diff-scale-b.json"
"$PYTHON" - "$TMP/diff-scale.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["algorithm"]["mode"] == "digest_sampled"
assert data["equivalence"]["equivalent"]
assert data["summary"]["drift_count"] == 10
changed = [
    item for item in data["algorithm"]["digest_evidence"]
    if item["identity"]["name"] == "app-02"
][0]
assert changed["final_recall_estimate"] == 1.0
assert changed["false_negative_upper_bound"] == 0.0
print(
    "scale-diff: digest_sampled, exact-equivalent, "
    f"triggers={len(data['algorithm']['triggers'])}"
)
PY

"$PYTHON" -m kdrift load --sample samples --db "$CRASH_DB" \
  >"$TMP/load-crash.json"
set +e
"$PYTHON" -m kdrift apply --db "$CRASH_DB" \
  --crash-at operation-start:2 >"$TMP/crash.json" 2>"$TMP/crash.err"
CRASH_CODE=$?
set -e
if [[ "$CRASH_CODE" -ne 137 ]]; then
  echo "expected crash exit 137, got $CRASH_CODE" >&2
  exit 1
fi
"$PYTHON" -m kdrift resume --db "$CRASH_DB" >"$TMP/resume.json"
"$PYTHON" - "$TMP/resume.json" <<'PY'
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
assert data["recovery"]["aborted_rounds"] == [1]
assert data["converged"]
assert data["verification"]["ok"]
print(
    "crash-resume: exit 137 observed, aborted round recovered, "
    "committed operations deduplicated, verify=ok"
)
PY

echo "demo-ok"

