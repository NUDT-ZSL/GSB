#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
OUT_DIR="${1:-$ROOT/demo_output}"

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR/run1" "$OUT_DIR/run2"

run_once() {
  local run_dir="$1"
  local db="$run_dir/state.db"
  "$PYTHON" -m alarm_dedup --db "$db" load --topology "$ROOT/samples/topology.json" --window-ms 1000 > "$run_dir/01_load.json"
  "$PYTHON" -m alarm_dedup --db "$db" ingest --events "$ROOT/samples/events.json" > "$run_dir/02_ingest.json"
  "$PYTHON" -m alarm_dedup --db "$db" status > "$run_dir/03_status_before_patch.json"
  "$PYTHON" -m alarm_dedup --db "$db" patch --patch "$ROOT/samples/patch.json" > "$run_dir/04_patch.json"
  "$PYTHON" -m alarm_dedup --db "$db" status > "$run_dir/05_status_after_patch.json"
  "$PYTHON" -m alarm_dedup --db "$db" patch --patch "$ROOT/samples/patch_revert.json" > "$run_dir/06_patch_revert.json"
  "$PYTHON" -m alarm_dedup --db "$db" status > "$run_dir/07_status_after_revert.json"
  "$PYTHON" -m alarm_dedup --db "$db" verify > "$run_dir/08_verify.json"
}

run_once "$OUT_DIR/run1"
run_once "$OUT_DIR/run2"

diff -u "$OUT_DIR/run1" "$OUT_DIR/run2" --exclude='*.db' --exclude='*.db-*' --exclude='*.lock' > "$OUT_DIR/diff.txt" || true
if [ -s "$OUT_DIR/diff.txt" ]; then
  printf 'determinism check failed\n' >&2
  cat "$OUT_DIR/diff.txt" >&2
  exit 1
fi

printf '{"ok":true,"deterministic":true,"output_dir":"%s"}\n' "$OUT_DIR"
