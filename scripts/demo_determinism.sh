#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/controlplane-demo.XXXXXX")"
trap 'rm -rf "$TMP"' EXIT
cd "$ROOT"

for RUN in run1 run2; do
  mkdir -p "$TMP/$RUN"
  DB="$TMP/$RUN/state.db"
  python3 -m controlplane --db "$DB" --sample-dir samples load > "$TMP/$RUN/load.json"
  python3 -m controlplane --db "$DB" --sample-dir samples distribute > "$TMP/$RUN/distribute.json"
  python3 -m controlplane --db "$DB" --sample-dir samples status > "$TMP/$RUN/status.json"
  python3 -m controlplane --db "$DB" --sample-dir samples explain > "$TMP/$RUN/explain.json"
  python3 -m controlplane --db "$DB" --sample-dir samples verify > "$TMP/$RUN/verify.json"
done

for REPORT in load distribute status explain verify; do
  if ! cmp -s "$TMP/run1/$REPORT.json" "$TMP/run2/$REPORT.json"; then
    echo "determinism failure: $REPORT" >&2
    exit 1
  fi
  HASH="$(sha256sum "$TMP/run1/$REPORT.json" | awk '{print $1}')"
  echo "identical $REPORT sha256=$HASH"
done

mkdir -p "$TMP/recovery"
DB="$TMP/recovery/state.db"
python3 -m controlplane --db "$DB" --sample-dir samples load > /dev/null
set +e
python3 -m controlplane --db "$DB" --sample-dir samples distribute --interrupt-after 3 > /dev/null
INTERRUPT_CODE=$?
set -e
if [[ "$INTERRUPT_CODE" -ne 75 ]]; then
  echo "interrupt hook returned $INTERRUPT_CODE instead of 75" >&2
  exit 1
fi
python3 -m controlplane --db "$DB" --sample-dir samples resume --report-work \
  > "$TMP/recovery/resume.json"
python3 -m controlplane --db "$DB" --sample-dir samples verify \
  > "$TMP/recovery/verify.json"

python3 - "$TMP" <<'PY'
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
distribute = json.loads((root / "run1/distribute.json").read_text())
resume = json.loads((root / "recovery/resume.json").read_text())
verify = json.loads((root / "recovery/verify.json").read_text())
print(
    "distribution",
    distribute["round_id"],
    distribute["status"],
    "clock=" + str(distribute["logical_clock"]),
    "dispatch=" + json.dumps(distribute["dispatch_counts"], sort_keys=True),
    "precision=" + str(distribute["precision_sensitive_total"]),
)
print(
    "recovery",
    resume["round_id"],
    resume["status"],
    "computed_this_run=" + str(resume["computed_this_run"]),
    "reused_this_run=" + str(resume["reused_this_run"]),
    "clock=" + str(resume["logical_clock"]),
)
print("verify_ok=" + str(verify["ok"]).lower())
PY
