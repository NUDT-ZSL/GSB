#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "== Python =="
python3 --version

echo "== Unit/integration tests =="
python3 -m pytest -q

WORKDIR="$(mktemp -d)"
echo "== Workdir: $WORKDIR =="

echo "== Deterministic generation =="
python3 -m radapt.cli generate --out "$WORKDIR/sample-a" >/dev/null
python3 -m radapt.cli generate --out "$WORKDIR/sample-b" >/dev/null
diff -u "$WORKDIR/sample-a/manifest.json" "$WORKDIR/sample-b/manifest.json"
echo "manifest reproduction: OK"

echo "== Continuous run =="
python3 -m radapt.cli run \
  --sample "$WORKDIR/sample-a" \
  --db "$WORKDIR/continuous.sqlite3" \
  --report "$WORKDIR/continuous-report.json" >"$WORKDIR/continuous-cli.json"

echo "== SIGKILL after committed batch 2 =="
set +e
python3 -m radapt.cli run \
  --sample "$WORKDIR/sample-a" \
  --db "$WORKDIR/kill.sqlite3" \
  --report "$WORKDIR/kill-report.json" \
  --crash-after-batch 2 >"$WORKDIR/kill-cli.json" 2>&1
KILL_RC=$?
set -e
echo "sigkill exit code: $KILL_RC"
test "$KILL_RC" -eq 137

python3 -m radapt.cli recover --db "$WORKDIR/kill.sqlite3" >"$WORKDIR/post-kill-snapshot.json"

echo "== Recovery run =="
python3 -m radapt.cli run \
  --sample "$WORKDIR/sample-a" \
  --db "$WORKDIR/kill.sqlite3" \
  --report "$WORKDIR/recovered-report.json" >"$WORKDIR/recovered-cli.json"

python3 - "$WORKDIR" <<'PY'
import json
import sqlite3
import sys
from pathlib import Path

work = Path(sys.argv[1])
continuous = json.loads((work / "continuous-report.json").read_text())
recovered = json.loads((work / "recovered-report.json").read_text())
assert all(x["matched"] for x in continuous["truth_verification"])
assert all(x["matched"] for x in recovered["truth_verification"])
assert recovered["skipped_committed_batches"] == [1, 2]
assert [x["seq"] for x in recovered["processed_batches"]] == [3, 4, 5, 6]
assert len(recovered["recovery_snapshot_final"]["drift_events"]) == 1
assert recovered["switch_decisions"][0]["switched"] is True
assert recovered["switch_decisions"][0]["precision_sensitive"] is True

def results(db_name):
    conn = sqlite3.connect(work / db_name)
    rows = conn.execute("SELECT window_id, result_json FROM window_state ORDER BY window_id").fetchall()
    conn.close()
    return {window_id: json.loads(payload) for window_id, payload in rows}

assert results("continuous.sqlite3") == results("kill.sqlite3")
print("continuous-vs-recovered aggregate values: MATCH")
drift = recovered["recovery_snapshot_final"]["drift_events"][0]
print("drift event:", drift["table_name"], drift["window_id"], drift["event_key"])
print("selected form:", recovered["switch_decisions"][0]["selected_assessment"]["form_id"])
print("VALIDATION_OK")
PY
