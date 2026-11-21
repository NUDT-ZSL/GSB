#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-$(mktemp -d "${TMPDIR:-/tmp}/infra-ledger-demo.XXXXXX")}"
SAMPLE="$ROOT/samples/default"
cd "$ROOT"

if [[ -e "$OUT/run-a" || -e "$OUT/run-b" ]]; then
  echo "output directory must be fresh: $OUT" >&2
  exit 1
fi

mkdir -p "$OUT/run-a" "$OUT/run-b"

run_pipeline() {
  local run_dir="$1"
  local db="$run_dir/ledger.sqlite3"
  python3 -m ledger_engine --db "$db" --sample "$SAMPLE" load > "$run_dir/load.json"
  python3 -m ledger_engine --db "$db" --sample "$SAMPLE" diagnose --workers 4 > "$run_dir/diagnose.json"
  python3 -m ledger_engine --db "$db" --sample "$SAMPLE" diagnose \
    --patch "$SAMPLE/patches/01-api-a-retry.json" > "$run_dir/diagnose-p1.json"
  python3 -m ledger_engine --db "$db" --sample "$SAMPLE" diagnose \
    --patch "$SAMPLE/patches/02-db-primary-capacity.json" > "$run_dir/diagnose-p2.json"
  python3 -m ledger_engine --db "$db" --sample "$SAMPLE" diagnose \
    --patch "$SAMPLE/patches/03-delete-api-a-retry.json" > "$run_dir/diagnose-p3.json"
  python3 -m ledger_engine --db "$db" status > "$run_dir/status.json"
  python3 -m ledger_engine --db "$db" verify > "$run_dir/verify.json"
}

run_pipeline "$OUT/run-a"
run_pipeline "$OUT/run-b"

for file in load.json diagnose.json diagnose-p1.json diagnose-p2.json diagnose-p3.json status.json verify.json; do
  cmp "$OUT/run-a/$file" "$OUT/run-b/$file"
  printf 'IDENTICAL %s\n' "$file"
done

printf 'DEMO OUTPUT: %s\n' "$OUT"
