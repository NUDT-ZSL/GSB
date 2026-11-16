#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BIN="${BIN:-$ROOT/target/debug/logstore}"
OUT="${1:-$ROOT/samples}"

if [[ ! -x "$BIN" ]]; then
  echo "binary not found: $BIN" >&2
  echo "run: cargo build" >&2
  exit 2
fi

"$BIN" generate-samples "$OUT"
