#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x .venv/bin/python ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3.11}"
fi

DB="$ROOT/data/demo.sqlite3"
READY="$ROOT/data/echo-ready.json"
mkdir -p "$ROOT/data"
"$PYTHON" - "$DB" "$READY" <<'PY'
from pathlib import Path
import sys
for name in sys.argv[1:]:
    path = Path(name)
    if path.exists():
        path.unlink()
PY

"$PYTHON" scripts/echo_server.py \
  --udp-port 39001 \
  --tcp-port 39002 \
  --ready-file "$READY" >"$ROOT/data/echo.log" 2>&1 &
ECHO_PID=$!
trap 'kill "$ECHO_PID" 2>/dev/null || true' EXIT

for _ in $(seq 1 100); do
  [[ -s "$READY" ]] && break
  sleep 0.05
done
[[ -s "$READY" ]] || { echo "echo service failed to start" >&2; exit 1; }

CONFIG=(
  --inventory "$ROOT/samples/peers.json"
  --links "$ROOT/samples/links.json"
  --policy "$ROOT/samples/policy.json"
  --db "$DB"
)

"$PYTHON" -m tunnel_kernel.cli load "${CONFIG[@]}"
"$PYTHON" -m tunnel_kernel.cli tunnel "${CONFIG[@]}" \
  --peer udp-alpha --payload-size 1024 --payload-seed 7 >"$ROOT/data/tunnel.json"
TUNNEL_ID="$("$PYTHON" -c \
  'import json,sys; print(json.load(open(sys.argv[1]))["tunnel_id"])' \
  "$ROOT/data/tunnel.json")"
"$PYTHON" -m tunnel_kernel.cli forward "${CONFIG[@]}" \
  --tunnel "$TUNNEL_ID" --scenario degrade-udp
"$PYTHON" -m tunnel_kernel.cli status "${CONFIG[@]}"
"$PYTHON" -m tunnel_kernel.cli verify "${CONFIG[@]}" --tunnel "$TUNNEL_ID"
