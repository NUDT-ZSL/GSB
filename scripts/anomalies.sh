#!/usr/bin/env bash
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$ROOT/target/debug/logstore}"
OUT="${OUT:-$ROOT/demo-output/anomalies}"
SAMPLES="$ROOT/samples/basic"

if [[ ! -x "$BIN" ]]; then
  echo "binary not found: $BIN; run cargo build" >&2
  exit 2
fi
if [[ ! -f "$SAMPLES/session.json" ]]; then
  "$BIN" generate-samples "$ROOT/samples" >/dev/null
fi

rm -rf "$OUT"
mkdir -p "$OUT"

echo "=== E_LOG: injected tail truncation repaired during recovery ==="
mkdir -p "$OUT/log/data"
cp -R "$SAMPLES/data/." "$OUT/log/data/"
printf 'quit\n' | "$BIN" --data-dir "$OUT/log/data" --session "$SAMPLES/session.json" --auto-recover --inject-tail-truncate 000002.log:8 | tee "$OUT/log.txt"
echo "exit_code=$?"

echo
echo "=== E_CORRUPT: checksum damage in a non-final segment ==="
mkdir -p "$OUT/corrupt/data"
cp -R "$SAMPLES/data/." "$OUT/corrupt/data/"
printf 'quit\n' | "$BIN" --data-dir "$OUT/corrupt/data" --inject-corrupt-checksum 000001.log:1 --auto-recover | tee "$OUT/corrupt.txt"
echo "exit_code=$?"

echo
echo "=== E_DEPEND: cycle injected before graph load ==="
mkdir -p "$OUT/depend/data"
printf 'quit\n' | "$BIN" --data-dir "$OUT/depend/data" --graph "$SAMPLES/graph.json" --inject-dependency-cycle --auto-recover | tee "$OUT/depend.txt"
echo "exit_code=$?"

echo
echo "=== E_SNAPSHOT: snapshot-referenced segment missing ==="
mkdir -p "$OUT/snapshot/data"
cp -R "$SAMPLES/data/." "$OUT/snapshot/data/"
printf 'quit\n' | "$BIN" --data-dir "$OUT/snapshot/data" --session "$SAMPLES/session.json" --inject-missing-snapshot-segment 000001.log | tee "$OUT/snapshot.txt"
echo "exit_code=$?"

echo
echo "anomaly output written to $OUT"
