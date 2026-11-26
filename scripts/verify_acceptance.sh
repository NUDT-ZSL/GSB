#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "[1/6] Generate deterministic samples and tree snapshots"
python run.py --mode generate

echo "[2/6] Run automated test suite"
python -m unittest tests.test_diagnostics

WORK="$(mktemp -d /tmp/prosody-acceptance-XXXXXX)"
CONTROL="$WORK/control"
RECOVERED="$WORK/recovered"
INVALIDATED="$WORK/invalidated"
OVERFLOW="$WORK/overflow"

echo "[3/6] Uninterrupted control run: $CONTROL"
python run.py --mode full --output-dir "$CONTROL" >/dev/null

echo "[4/6] Hard interrupt and resume: $RECOVERED"
set +e
python run.py --mode full --output-dir "$RECOVERED" --interrupt-at adjudication:2 >/dev/null 2>"$WORK/interrupt.err"
INTERRUPT_CODE=$?
set -e
if [[ "$INTERRUPT_CODE" -ne 37 ]]; then
  echo "expected interrupt exit code 37, got $INTERRUPT_CODE" >&2
  exit 1
fi
python run.py --mode full --output-dir "$RECOVERED" >/dev/null
diff -r --exclude=last_run_plan.json "$CONTROL" "$RECOVERED"

echo "[5/6] Targeted revision invalidation: $INVALIDATED"
python run.py --mode full --output-dir "$INVALIDATED" >/dev/null
python run.py --mode full --output-dir "$INVALIDATED" --bump-revision U002 >/dev/null
python - "$INVALIDATED" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads((Path(sys.argv[1]) / "cache_revalidation.json").read_text())
assert plan["recomputed_utterance_ids"] == ["U002"], plan
assert plan["reused_utterance_ids"] == [
    "U001", "U003", "U004", "U005", "U006"
], plan
print("invalidation plan OK")
PY

echo "[6/6] Conflict overflow and default artifact run"
set +e
python run.py --mode full --output-dir "$OVERFLOW" --conflict-threshold 0 >/dev/null 2>"$WORK/overflow.err"
OVERFLOW_CODE=$?
set -e
if [[ "$OVERFLOW_CODE" -ne 2 ]] || ! grep -q E_CONFLICT_OVERFLOW "$WORK/overflow.err"; then
  echo "expected E_CONFLICT_OVERFLOW, exit=$OVERFLOW_CODE" >&2
  cat "$WORK/overflow.err" >&2
  exit 1
fi
python run.py --mode full

echo "ACCEPTANCE_OK workdir=$WORK"
