#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x "$ROOT/.venv311/bin/python" ]]; then
  PYTHON="$ROOT/.venv311/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi

WORK_DIR="${WORK_DIR:-$ROOT/work/demo}"
mkdir -p "$WORK_DIR/results"
DB="$WORK_DIR/state.sqlite"
SCENE="$ROOT/samples/scene.json"
MOCK_CONFIG="$ROOT/samples/mock_service.json"

rm -f "$DB" "$DB-wal" "$DB-shm" "$DB.lock"

"$PYTHON" -m capacity_engine.mock_service --config "$MOCK_CONFIG" --port 18080 \
  >"$WORK_DIR/mock-service.log" 2>&1 &
MOCK_PID=$!
trap 'kill "$MOCK_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  if "$PYTHON" - <<'PY'
import urllib.request
try:
    with urllib.request.urlopen("http://127.0.0.1:18080/health", timeout=0.2) as response:
        raise SystemExit(0 if response.status == 200 else 1)
except Exception:
    raise SystemExit(1)
PY
  then
    break
  fi
  sleep 0.05
done

"$PYTHON" -m capacity_engine run \
  --scene "$SCENE" \
  --db "$DB" \
  --results "$WORK_DIR/results" \
  | tee "$WORK_DIR/run-summary.json"

"$PYTHON" -m capacity_engine verify --db "$DB" \
  | tee "$WORK_DIR/verification.json"

"$PYTHON" -m capacity_engine report --db "$DB" \
  --output "$WORK_DIR/results/report.json" \
  >/dev/null

"$PYTHON" -m capacity_engine status --db "$DB" --format text \
  | tee "$WORK_DIR/status.txt"

