#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi
OUT="${1:-$ROOT/artifacts/demo}"
mkdir -p "$OUT"

"$PYTHON" - "$OUT" <<'PY'
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
for pattern in ("*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.sqlite.lock"):
    for path in out.glob(pattern):
        path.unlink(missing_ok=True)
for name in (
    "run-a.json",
    "run-b.json",
    "report-a.json",
    "report-b.json",
    "verify-a.json",
    "verify-b.json",
    "crash-run.json",
    "crash-run.err",
    "crash-status.json",
    "resume.json",
    "report-resumed.json",
    "verify-resumed.json",
):
    (out / name).unlink(missing_ok=True)
PY

SCENE_ARGS=(
  --link "$ROOT/samples/link.json"
  --script "$ROOT/samples/script.json"
  --congestion "$ROOT/samples/congestion.json"
  --observations "$ROOT/samples/observations.json"
)

"$PYTHON" -m tcp_engine run "${SCENE_ARGS[@]}" \
  --db "$OUT/run-a.sqlite" --run-id demo > "$OUT/run-a.json"
"$PYTHON" -m tcp_engine run "${SCENE_ARGS[@]}" \
  --db "$OUT/run-b.sqlite" --run-id demo > "$OUT/run-b.json"

cmp -s "$OUT/run-a.json" "$OUT/run-b.json"

"$PYTHON" -m tcp_engine report \
  --db "$OUT/run-a.sqlite" --run-id demo > "$OUT/report-a.json"
"$PYTHON" -m tcp_engine report \
  --db "$OUT/run-b.sqlite" --run-id demo > "$OUT/report-b.json"

cmp -s "$OUT/report-a.json" "$OUT/report-b.json"

"$PYTHON" -m tcp_engine verify "${SCENE_ARGS[@]}" \
  --db "$OUT/run-a.sqlite" --run-id demo > "$OUT/verify-a.json"
"$PYTHON" -m tcp_engine verify "${SCENE_ARGS[@]}" \
  --db "$OUT/run-b.sqlite" --run-id demo > "$OUT/verify-b.json"

set +e
TCP_ENGINE_CRASH_AFTER=7 "$PYTHON" -m tcp_engine run "${SCENE_ARGS[@]}" \
  --db "$OUT/crash.sqlite" --run-id demo \
  > "$OUT/crash-run.json" 2> "$OUT/crash-run.err"
CRASH_CODE=$?
set -e
if [[ "$CRASH_CODE" -ne 70 ]]; then
  echo "expected crash hook exit code 70, got $CRASH_CODE" >&2
  exit 1
fi

"$PYTHON" -m tcp_engine status \
  --db "$OUT/crash.sqlite" --run-id demo > "$OUT/crash-status.json"
"$PYTHON" -m tcp_engine resume "${SCENE_ARGS[@]}" \
  --db "$OUT/crash.sqlite" --run-id demo > "$OUT/resume.json"
"$PYTHON" -m tcp_engine report \
  --db "$OUT/crash.sqlite" --run-id demo > "$OUT/report-resumed.json"
"$PYTHON" -m tcp_engine verify "${SCENE_ARGS[@]}" \
  --db "$OUT/crash.sqlite" --run-id demo > "$OUT/verify-resumed.json"

cmp -s "$OUT/report-a.json" "$OUT/report-resumed.json"

"$PYTHON" - "$OUT" <<'PY'
import hashlib
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])


def load(name):
    return json.loads((out / name).read_text(encoding="utf-8"))


def sha256(name):
    return hashlib.sha256((out / name).read_bytes()).hexdigest()


report = load("report-a.json")
status = load("crash-status.json")
resume = load("resume.json")
verify = load("verify-resumed.json")
summary = {
    "crash_committed_events": status["latest"]["committed_events"],
    "crash_resume_replayed_events": resume["events_executed_this_call"],
    "demo": "deterministic-tcp-demo",
    "report_sha256": sha256("report-a.json"),
    "reports_byte_identical": True,
    "resumed_report_matches_clean_report": True,
    "run_outputs_byte_identical": True,
    "source_digest": report["source_digest"],
    "trace_digest": report["trace_digest"],
    "verified_events": verify["verified_events"],
}
print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
PY

