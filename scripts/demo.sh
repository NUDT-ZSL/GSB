#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PYTHON="${PYTHON:-python3}"
RUNTIME="$ROOT/.runtime"
BASE_STATE="$RUNTIME/base-state"
CRASH_STATE="$RUNTIME/crash-state"

mkdir -p "$RUNTIME" "$ROOT/artifacts"

load_args=(
  load
  --model samples/model.json
  --replicas samples/replicas.json
  --requests samples/requests.json
  --precision samples/precision.json
  --reset
)

"$PYTHON" -m batchkernel.cli --state-dir "$BASE_STATE" "${load_args[@]}" >/dev/null
"$PYTHON" -m batchkernel.cli --state-dir "$BASE_STATE" schedule --clock 1200 >/dev/null
"$PYTHON" -m batchkernel.cli --state-dir "$BASE_STATE" status >artifacts/demo-status.json
"$PYTHON" -m batchkernel.cli --state-dir "$BASE_STATE" verify >artifacts/demo-verify.json

"$PYTHON" -m batchkernel.cli --state-dir "$CRASH_STATE" "${load_args[@]}" >/dev/null
set +e
BATCHKERNEL_FAULT=after_batch_commit \
  "$PYTHON" -m batchkernel.cli --state-dir "$CRASH_STATE" schedule --clock 1200 \
  >artifacts/demo-crash.json
crash_exit=$?
set -e

"$PYTHON" -m batchkernel.cli --state-dir "$CRASH_STATE" resume \
  >artifacts/demo-resume.json
"$PYTHON" -m batchkernel.cli --state-dir "$CRASH_STATE" status \
  >artifacts/demo-crash-status.json
"$PYTHON" -m batchkernel.cli --state-dir "$CRASH_STATE" verify \
  >artifacts/demo-crash-verify.json

"$PYTHON" - "$crash_exit" <<'PY'
import json
import sys
from pathlib import Path

root = Path.cwd()
base = json.loads((root / "artifacts/demo-status.json").read_text())["status"]
crashed = json.loads((root / "artifacts/demo-crash-status.json").read_text())["status"]
verify = json.loads((root / "artifacts/demo-verify.json").read_text())
crash_verify = json.loads((root / "artifacts/demo-crash-verify.json").read_text())
summary = {
    "crash_exit_code": int(sys.argv[1]),
    "status_identical_after_resume": base == crashed,
    "baseline_verify_ok": verify["ok"],
    "recovered_verify_ok": crash_verify["ok"],
    "clock": base["clock"],
    "request_state_counters": base["request_state_counters"],
    "sensitive_batches": sorted(
        [
            {
                "replica_id": replica["replica_id"],
                "window_index": window["window_index"],
                "batch_index": batch["batch_index"],
                "request_seqs": batch["request_seqs"],
                "sensitive_decisions": [
                    {
                        "decision": decision["decision"],
                        "metric": decision["metric"],
                        "absolute_gap": decision["absolute_gap"],
                    }
                    for decision in batch["precision_sensitive_decisions"]
                ],
            }
            for replica in base["replicas"]
            for window in replica["windows"]
            for batch in window["batches"]
            if batch["precision_sensitive"]
        ],
        key=lambda item: (
            item["replica_id"],
            item["window_index"],
            item["batch_index"],
        ),
    ),
}
(root / "artifacts/demo-summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    encoding="utf-8",
)
print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
PY

