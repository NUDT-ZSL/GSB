#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
STATE="${ROOT}/artifacts/force_kill/state"
REPORT="${ROOT}/artifacts/force_kill/run_should_not_exist.json"
RESUMED="${ROOT}/artifacts/force_kill/resumed.json"
if [[ ! -x "${PY}" ]]; then
  echo "Missing ${PY}; create .venv and install requirements.txt first." >&2
  exit 1
fi
cd "${ROOT}"
"${PY}" - <<'PY'
import shutil
from pathlib import Path
path = Path("artifacts/force_kill")
if path.exists():
    shutil.rmtree(path)
path.mkdir(parents=True)
PY
"${PY}" -m convedit init \
  --graph samples/graph.json \
  --input samples/input.json \
  --config samples/config_force_kill.json \
  --state "${STATE}" \
  --report "${ROOT}/artifacts/force_kill/init.json"
"${PY}" -m convedit run \
  --state "${STATE}" \
  --report "${REPORT}" &
PID=$!
# conv1/conv3 and both interpolations finish first. conv2 then sleeps 1.5 s;
# SIGKILL lands while conv2 is running and before its product/snapshot exists.
sleep 0.8
kill -9 "${PID}" 2>/dev/null || true
set +e
wait "${PID}"
RC=$?
set -e
echo "hard-kill return code: ${RC}"
if [[ "${RC}" -ne 137 && "${RC}" -ne -9 ]]; then
  echo "expected SIGKILL return code, got ${RC}" >&2
  exit 1
fi
if [[ -f "${REPORT}" ]]; then
  echo "unexpected final report after SIGKILL: ${REPORT}" >&2
  exit 1
fi
"${PY}" - <<'PY'
import json
from pathlib import Path
snapshot = json.loads(Path("artifacts/force_kill/state/current.json").read_text(encoding="utf-8"))
print("snapshot stage:", snapshot["stage"])
print("completed nodes:", snapshot["completed_nodes"])
print("pending/reused status:", snapshot["node_status"])
assert len(snapshot["completed_nodes"]) == 4, snapshot["completed_nodes"]
assert snapshot["node_status"]["conv2"] == "pending"
PY
"${PY}" -m convedit resume \
  --state "${STATE}" \
  --report "${RESUMED}"
"${PY}" - <<'PY'
import json
from pathlib import Path
report = json.loads(Path("artifacts/force_kill/resumed.json").read_text(encoding="utf-8"))
print("resumed status:", report["status"])
print("reused:", report["reused_nodes"])
print("recalculated:", report["recalculated_nodes"])
assert report["status"] == "complete"
assert report["reused_nodes"] == ["conv1", "conv3", "interp_bi", "interp_nn"]
assert report["recalculated_nodes"] == ["conv2"]
PY
