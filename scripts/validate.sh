#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="$ROOT/venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi

echo "== compileall =="
"$PYTHON" -m compileall -q gemm_bmm_workbench tests

echo "== unittest =="
"$PYTHON" -m unittest discover -s tests

echo "== deterministic demo =="
"$PYTHON" -m gemm_bmm_workbench.cli demo --workspace "$ROOT" --state-dir "$ROOT/runs/demo"

echo "== SIGKILL recovery demo =="
"$PYTHON" -m gemm_bmm_workbench.cli recovery-demo --workspace "$ROOT" --state-dir "$ROOT/runs/recovery-demo"

echo "== selfcheck =="
"$PYTHON" -m gemm_bmm_workbench.cli selfcheck --state-dir "$ROOT/runs/demo"
