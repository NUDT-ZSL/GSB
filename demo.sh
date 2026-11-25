#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${1:-$ROOT/artifacts/demo}"

mkdir -p "$WORK"
rm -f "$WORK"/ledger.db "$WORK"/ledger.db-wal "$WORK"/ledger.db-shm "$WORK"/ledger.db.lock

python3 "$ROOT/generate_samples.py" \
  --output "$ROOT/samples/default" \
  --seed 20261126 > "$WORK/generate.json"

python3 "$ROOT/ledger.py" load "$ROOT/samples/default" --db "$WORK/ledger.db" > "$WORK/load.json"
python3 "$ROOT/ledger.py" inject \
  --db "$WORK/ledger.db" \
  --window-index 0 \
  --phase batch_commit \
  --batch-index 0 > "$WORK/inject.json"

set +e
python3 "$ROOT/ledger.py" run --db "$WORK/ledger.db" > "$WORK/run-killed.json" 2> "$WORK/run-killed.err"
RUN_EXIT=$?
set -e
if [[ "$RUN_EXIT" -ne 137 && "$RUN_EXIT" -ne 9 ]]; then
  echo "expected SIGKILL, got exit $RUN_EXIT" >&2
  exit 1
fi

python3 "$ROOT/ledger.py" status --db "$WORK/ledger.db" > "$WORK/partial-status.json"
python3 "$ROOT/ledger.py" resume --db "$WORK/ledger.db" > "$WORK/resume.json"
python3 "$ROOT/ledger.py" verify --db "$WORK/ledger.db" > "$WORK/verify.json"

python3 - "$WORK" "$RUN_EXIT" <<'PY'
import json
import pathlib
import sys

work = pathlib.Path(sys.argv[1])
run_exit = int(sys.argv[2])
partial = json.loads((work / "partial-status.json").read_text())
resumed = json.loads((work / "resume.json").read_text())
verified = json.loads((work / "verify.json").read_text())
summary = {
    "ok": bool(verified["ok"]),
    "run_exit": run_exit,
    "partial_complete": partial["complete"],
    "half_committed_windows": partial["progress"].get("half_committed_windows", 0),
    "fired_injections": sum(1 for item in partial["injections"] if item["fired"]),
    "resume_complete": resumed["complete"],
    "request_states": resumed["request_states"],
    "verify_bitwise_match": verified["bitwise_match"],
    "verify_difference_count": verified["difference_count"],
    "internal_checks_passed": verified["internal_checks"]["passed"],
}
print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
if not summary["ok"]:
    raise SystemExit(1)
PY
