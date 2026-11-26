#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

FIXTURES="${1:-fixtures/seed}"
BASE="$(mktemp -d /tmp/diag-matrix-XXXXXX)"
trap 'status=$?; if [[ $status -ne 0 ]]; then echo "matrix failed; artifacts=$BASE" >&2; fi' EXIT

python3 - "$FIXTURES" >"$BASE/ids.txt" <<'PY'
import json
import sys
from pathlib import Path

document = json.loads((Path(sys.argv[1]) / "injections" / "fault_injections.json").read_text())
for case in document["cases"]:
    print(case["injection_id"])
PY

count=0
while IFS= read -r injection_id; do
  state="$BASE/$injection_id"
  set +e
  python3 -m diagnostic_engine.cli inject \
    --fixtures "$FIXTURES" --state-dir "$state" \
    --case "$injection_id" >"$state.inj.json" 2>"$state.inj.err"
  inject_rc=$?
  set -e
  if [[ "$inject_rc" -ne 2 ]]; then
    echo "$injection_id expected safe injection exit 2, got $inject_rc" >&2
    exit 1
  fi

  python3 -m diagnostic_engine.cli resume \
    --fixtures "$FIXTURES" --state-dir "$state" \
    --case "$injection_id" >"$state.resume.json"
  python3 - "$state.resume.json" "$injection_id" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if not payload.get("ok"):
    raise SystemExit(f"resume invariants failed for {sys.argv[2]}: {payload}")
PY
  echo "PASS $injection_id"
  count=$((count + 1))
done <"$BASE/ids.txt"

echo "MATRIX_OK $count/13 artifacts=$BASE"
