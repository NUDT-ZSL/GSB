#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

OUT="${1:-artifacts/demo}"
PYTHON="${PYTHON:-}"

if [[ -z "$PYTHON" ]]; then
  for candidate in python3.12 python3.11 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      version="$("$candidate" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
      major="${version%%.*}"
      minor="${version##*.}"
      if (( major > 3 || (major == 3 && minor >= 11) )); then
        PYTHON="$candidate"
        break
      fi
    fi
  done
fi

if [[ -z "$PYTHON" ]]; then
  echo "未找到 Python 3.11+；可设置 PYTHON=/path/to/python3.12" >&2
  exit 1
fi

export PYTHONUTF8=1
"$PYTHON" -m kgpipe demo --out "$OUT"
echo "演示结果: $OUT/demo-summary.json"

