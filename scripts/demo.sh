#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
RUN_DIR="$(mktemp -d "$ROOT/state/demo.XXXXXX")"

echo "demo directory: $RUN_DIR"

echo
echo "== basic scene: run =="
"$PYTHON" -m quicsim run \
  --scene "$ROOT/data/basic_scene.json" \
  --db "$RUN_DIR/basic.sqlite" \
  --commit-every 5 | tee "$RUN_DIR/basic-run.json"

echo
echo "== basic scene: status =="
"$PYTHON" -m quicsim status --db "$RUN_DIR/basic.sqlite" \
  | tee "$RUN_DIR/basic-status.json"

echo
echo "== basic scene: verify =="
"$PYTHON" -m quicsim verify \
  --db "$RUN_DIR/basic.sqlite" \
  --scene "$ROOT/data/basic_scene.json" \
  | tee "$RUN_DIR/basic-verify.json"

echo
echo "== loss/PTO scene: run and verify =="
"$PYTHON" -m quicsim run \
  --scene "$ROOT/data/loss_pto_scene.json" \
  --db "$RUN_DIR/loss.sqlite" \
  --commit-every 3 | tee "$RUN_DIR/loss-run.json"
"$PYTHON" -m quicsim verify \
  --db "$RUN_DIR/loss.sqlite" \
  --scene "$ROOT/data/loss_pto_scene.json" \
  | tee "$RUN_DIR/loss-verify.json"

echo
echo "== forced termination after two committed checkpoints =="
set +e
"$PYTHON" -m quicsim run \
  --scene "$ROOT/data/basic_scene.json" \
  --db "$RUN_DIR/recovery.sqlite" \
  --commit-every 2 \
  --crash-after 2 | tee "$RUN_DIR/crash.json"
CRASH_CODE=${PIPESTATUS[0]}
set -e
if [[ "$CRASH_CODE" -ne 137 ]]; then
  echo "expected injected crash exit code 137, got $CRASH_CODE" >&2
  exit 1
fi

echo
echo "== resume after the crash =="
"$PYTHON" -m quicsim resume \
  --scene "$ROOT/data/basic_scene.json" \
  --db "$RUN_DIR/recovery.sqlite" \
  --commit-every 4 | tee "$RUN_DIR/resume.json"

echo
echo "== recovery verification =="
"$PYTHON" -m quicsim verify \
  --db "$RUN_DIR/recovery.sqlite" \
  --scene "$ROOT/data/basic_scene.json" \
  | tee "$RUN_DIR/recovery-verify.json"

echo
echo "demo complete; evidence files: $RUN_DIR"

