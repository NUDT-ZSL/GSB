#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIR="$(mktemp -d "${TMPDIR:-/tmp}/infra-ledger-crash.XXXXXX")"
SAMPLE="$ROOT/samples/default"
DB="$DIR/ledger.sqlite3"
cd "$ROOT"

python3 -m ledger_engine --db "$DB" --sample "$SAMPLE" load >/dev/null
set +e
LEDGER_CRASH_AFTER_STAGED_SERVICE=3 \
  python3 -m ledger_engine --db "$DB" --sample "$SAMPLE" diagnose --workers 2 >/dev/null
status=$?
set -e
if [[ "$status" -ne 137 ]]; then
  echo "expected crash exit 137, got $status" >&2
  exit 1
fi

python3 -m ledger_engine --db "$DB" status > "$DIR/status-after-crash.json"
python3 -m ledger_engine --db "$DB" --sample "$SAMPLE" resume > "$DIR/resume.json"
python3 -m ledger_engine --db "$DB" verify > "$DIR/verify.json"
cat "$DIR/resume.json"
printf 'CRASH RECOVERY OK: %s\n' "$DIR"
