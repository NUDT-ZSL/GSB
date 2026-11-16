#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$ROOT/target/debug/logstore}"
OUT="${OUT:-$ROOT/demo-output}"
SAMPLES="$ROOT/samples/basic"

if [[ ! -x "$BIN" ]]; then
  echo "binary not found: $BIN; run cargo build" >&2
  exit 2
fi
if [[ ! -f "$SAMPLES/session.json" ]]; then
  "$BIN" generate-samples "$ROOT/samples"
fi

rm -rf "$OUT"
mkdir -p "$OUT"

echo "=== scenario 1: first recovery from snapshot ==="
mkdir -p "$OUT/scenario1"
cp -R "$SAMPLES/data" "$OUT/scenario1/data"
printf 'views\ntrace\nquit\n' | \
  "$BIN" --data-dir "$OUT/scenario1/data" \
          --session "$SAMPLES/session.json" \
          --auto-recover | tee "$OUT/scenario1.txt"

echo
echo "=== scenario 2: incremental write, snapshot point, validation ==="
mkdir -p "$OUT/scenario2"
cp -R "$SAMPLES/data" "$OUT/scenario2/data"
printf 'put user:cn:carol Carol\nsnapshot\nsnapshot-save %s\nvalidate\nsegments\nviews\ntrace\nquit\n' \
  "$OUT/scenario2/session.json" | \
  "$BIN" --data-dir "$OUT/scenario2/data" \
          --session "$SAMPLES/session.json" \
          --auto-recover | tee "$OUT/scenario2.txt"

echo
echo "=== scenario 3: injected tail truncation and recovery ==="
mkdir -p "$OUT/scenario3"
cp -R "$SAMPLES/data" "$OUT/scenario3/data"
printf 'views\ntrace\nquit\n' | \
  "$BIN" --data-dir "$OUT/scenario3/data" \
          --session "$SAMPLES/session.json" \
          --auto-recover \
          --inject-tail-truncate 000002.log:8 | tee "$OUT/scenario3.txt"

echo
echo "demo output written to $OUT"
