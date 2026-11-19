#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi
OUT="${1:-$ROOT/artifacts/concurrent}"
mkdir -p "$OUT"

"$PYTHON" - "$OUT" <<'PY'
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
for pattern in ("*.sqlite", "*.sqlite-wal", "*.sqlite-shm", "*.sqlite.lock", "worker-*.log"):
    for path in out.glob(pattern):
        path.unlink(missing_ok=True)
for name in ("status.json", "verify.json", "report.json", "duplicate.json"):
    (out / name).unlink(missing_ok=True)
PY

SCENE_ARGS=(
  --link "$ROOT/samples/link.json"
  --script "$ROOT/samples/script.json"
  --congestion "$ROOT/samples/congestion.json"
  --observations "$ROOT/samples/observations.json"
)

PIDS=()
BOUNDS=("0 6" "6 12" "12 18" "18 24")
for index in "${!BOUNDS[@]}"; do
  read -r START END <<< "${BOUNDS[$index]}"
  "$PYTHON" -m tcp_engine run "${SCENE_ARGS[@]}" \
    --db "$OUT/parallel.sqlite" --run-id demo \
    --segment-start "$START" --segment-end "$END" \
    --version-tag parallel > "$OUT/worker-$index.log" 2>&1 &
  PIDS+=("$!")
done

for pid in "${PIDS[@]}"; do
  wait "$pid"
done

"$PYTHON" -m tcp_engine status \
  --db "$OUT/parallel.sqlite" --run-id demo > "$OUT/status.json"
"$PYTHON" -m tcp_engine run "${SCENE_ARGS[@]}" \
  --db "$OUT/parallel.sqlite" --run-id demo \
  --segment-start 0 --segment-end 6 --version-tag parallel \
  > "$OUT/duplicate.json"
"$PYTHON" -m tcp_engine verify "${SCENE_ARGS[@]}" \
  --db "$OUT/parallel.sqlite" --run-id demo > "$OUT/verify.json"
"$PYTHON" -m tcp_engine report \
  --db "$OUT/parallel.sqlite" --run-id demo > "$OUT/report.json"

"$PYTHON" - "$OUT" <<'PY'
import json
import pathlib
import sys

out = pathlib.Path(sys.argv[1])
status = json.loads((out / "status.json").read_text(encoding="utf-8"))
verify = json.loads((out / "verify.json").read_text(encoding="utf-8"))
report = json.loads((out / "report.json").read_text(encoding="utf-8"))
duplicate = json.loads((out / "duplicate.json").read_text(encoding="utf-8"))
summary = {
    "active_version_status": status["active"]["status"],
    "committed_events": status["active"]["terminal_events"],
    "duplicate_submit_idempotent": duplicate["complete"],
    "parallel_segments_completed": True,
    "trace_digest": report["trace_digest"],
    "verified_events": verify["verified_events"],
}
print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
PY

