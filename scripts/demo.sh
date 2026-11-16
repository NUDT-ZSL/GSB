#!/usr/bin/env bash
# Reproducible demo: normal step, delayed step, snapshot + kill -9 + restore.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m ddp_sim ports || {
  echo "port range busy, cleaning up..." >&2
  fuser -k 29500/tcp 29501/tcp 29502/tcp 29503/tcp 2>/dev/null || true
  sleep 1
}
exec python3 -m ddp_sim demo
