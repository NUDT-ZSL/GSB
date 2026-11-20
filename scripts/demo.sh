#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -z "${PYTHON:-}" && -x ".venv/bin/python" ]]; then
  PYTHON=".venv/bin/python"
else
  PYTHON="${PYTHON:-python3}"
fi
STATE_DIR="$(mktemp -d /tmp/shardpipe-demo.XXXXXX)"

echo "state_dir=$STATE_DIR"

"$PYTHON" scripts/init_workspace.py \
  --output data/sample.json --seed 20261121

"$PYTHON" -m shardpipe load \
  --input data/sample.json --state-dir "$STATE_DIR"

"$PYTHON" -m shardpipe schedule \
  --state-dir "$STATE_DIR" --job train-alpha --run-id demo-alpha-1

"$PYTHON" -m shardpipe verify \
  --state-dir "$STATE_DIR" --run-id demo-alpha-1

"$PYTHON" -m shardpipe schedule \
  --state-dir "$STATE_DIR" --job train-alpha --run-id demo-alpha-2

"$PYTHON" -m shardpipe status \
  --state-dir "$STATE_DIR" --run-id demo-alpha-2

echo "demo_state=$STATE_DIR"
