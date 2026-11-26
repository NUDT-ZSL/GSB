#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FIXTURES="fixtures/seed"
python3 generate_fixtures.py --output "$FIXTURES" >/tmp/diag-seed.json

WORK="$(mktemp -d /tmp/diag-acceptance-XXXXXX)"
echo "workdir=$WORK"

python3 -m diagnostic_engine.cli load --fixtures "$FIXTURES" >"$WORK/load.json"
python3 -m diagnostic_engine.cli run \
  --fixtures "$FIXTURES" --state-dir "$WORK/baseline" --policy both \
  >"$WORK/run-both.json"
python3 -m diagnostic_engine.cli verify \
  --fixtures "$FIXTURES" --state-dir "$WORK/baseline" --expect-consistent \
  >"$WORK/verify-baseline.json"

set +e
python3 -m diagnostic_engine.cli inject \
  --fixtures "$FIXTURES" --state-dir "$WORK/hard-orphan" \
  --case INJ-MF-RENAME-KILL --hard-kill \
  >"$WORK/hard-inject.out" 2>"$WORK/hard-inject.err"
hard_rc=$?
set -e
if [[ "$hard_rc" -ne 137 ]]; then
  echo "expected SIGKILL exit status 137, got $hard_rc" >&2
  exit 1
fi
python3 -m diagnostic_engine.cli resume \
  --fixtures "$FIXTURES" --state-dir "$WORK/hard-orphan" \
  --case INJ-MF-RENAME-KILL >"$WORK/hard-resume.json"
python3 -m diagnostic_engine.cli verify \
  --fixtures "$FIXTURES" --state-dir "$WORK/hard-orphan" \
  >"$WORK/hard-verify.json"

set +e
python3 -m diagnostic_engine.cli inject \
  --fixtures "$FIXTURES" --state-dir "$WORK/hard-log" \
  --case INJ-LOG-APPEND-KILL --hard-kill \
  >"$WORK/log-inject.out" 2>"$WORK/log-inject.err"
log_rc=$?
set -e
if [[ "$log_rc" -ne 137 ]]; then
  echo "expected log SIGKILL exit status 137, got $log_rc" >&2
  exit 1
fi
python3 -m diagnostic_engine.cli resume \
  --fixtures "$FIXTURES" --state-dir "$WORK/hard-log" \
  --case INJ-LOG-APPEND-KILL >"$WORK/log-resume.json"
python3 -m diagnostic_engine.cli verify \
  --fixtures "$FIXTURES" --state-dir "$WORK/hard-log" \
  >"$WORK/log-verify.json"

for index in 0 1 2 3; do
  python3 -m diagnostic_engine.cli run \
    --fixtures "$FIXTURES" --state-dir "$WORK/parallel" \
    --policy metadata_first --shard-index "$index" --shard-count 4 \
    --worker-id "acceptance-$index" >"$WORK/parallel-$index.json" &
done
wait
python3 -m diagnostic_engine.cli verify \
  --fixtures "$FIXTURES" --state-dir "$WORK/parallel" \
  >"$WORK/parallel-verify.json"

python3 - "$WORK" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

work = Path(sys.argv[1])

baseline_run = json.loads((work / "run-both.json").read_text())
assert baseline_run["ok"] is True
assert baseline_run["comparison"]["consistent"] is True
assert baseline_run["comparison"]["first_divergence_object_key"] is None

baseline_verify = json.loads((work / "verify-baseline.json").read_text())
assert baseline_verify["ok"] is True, baseline_verify

hard_resume = json.loads((work / "hard-resume.json").read_text())
assert hard_resume["ok"] is True
hard_result = hard_resume["recovery"]["results"][0]
assert hard_result["crash_verdict"] == "orphan_object"
assert hard_result["result"]["status"] == "completed"
hard_verify = json.loads((work / "hard-verify.json").read_text())
assert hard_verify["ok"] is True, hard_verify

log_resume = json.loads((work / "log-resume.json").read_text())
assert log_resume["ok"] is True
log_result = log_resume["recovery"]["results"][0]
assert log_result["log_outcome"] == "complete"
assert log_result["crash_verdict"] == "dirty_cache"
log_verify = json.loads((work / "log-verify.json").read_text())
assert log_verify["ok"] is True, log_verify

parallel_verify = json.loads((work / "parallel-verify.json").read_text())
assert parallel_verify["ok"] is True, parallel_verify
connection = sqlite3.connect(work / "parallel" / "metadata_first" / "ledger.sqlite")
try:
    claims = connection.execute("SELECT COUNT(*) FROM object_claims").fetchone()[0]
    duplicates = connection.execute(
        "SELECT COUNT(*) FROM (SELECT 1 FROM object_claims "
        "GROUP BY object_key, content_version, claim_scope HAVING COUNT(*) > 1)"
    ).fetchone()[0]
finally:
    connection.close()
assert claims == 16, claims
assert duplicates == 0, duplicates

print("ACCEPTANCE_ASSERTIONS_PASSED")
print(f"baseline_consistent=true")
print(f"hard_orphan_recovered=true")
print(f"complete_log_dirty_recovered=true")
print(f"parallel_claims={claims}")
PY

echo "ACCEPTANCE_OK $WORK"
