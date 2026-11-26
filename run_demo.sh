#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

OUT="$ROOT/artifacts/validation"
mkdir -p "$OUT"
PY=(python3 -m kvledger.cli)

python3 - <<'PY' > "$OUT/00-environment.json"
import json
import platform
import sqlite3
import sys

try:
    import numpy as np
    numpy_version = np.__version__
except Exception as exc:  # pragma: no cover
    numpy_version = f"unavailable:{exc}"

print(json.dumps({
    "python": sys.version.split()[0],
    "platform": platform.platform(),
    "numpy": numpy_version,
    "sqlite": sqlite3.sqlite_version,
}, indent=2, sort_keys=True))
PY

echo "[1/8] deterministic samples"
"${PY[@]}" generate-samples --out samples > "$OUT/01-seed.json"

WORK_ROOT="$OUT/workspaces-$$"
mkdir -p "$WORK_ROOT"
NORMAL_ROOT="$WORK_ROOT/normal"
CRASH_ROOT="$WORK_ROOT/crash"
PARALLEL_ROOT="$WORK_ROOT/parallel"
POLICY_ROOT="$WORK_ROOT/policy"
FAILURE_ROOT="$WORK_ROOT/failure"

echo "[2/8] normal load/merge/eviction"
"${PY[@]}" --workspace "$NORMAL_ROOT" load \
  --requests samples/requests.json \
  --config samples/memory_config.json > "$OUT/02-normal-load.json"
"${PY[@]}" --workspace "$NORMAL_ROOT" merge \
  --package samples/policy_package > "$OUT/03-normal-merge.json"
for i in $(seq 1 14); do
  "${PY[@]}" --workspace "$NORMAL_ROOT" evict --round-id "R$i" \
    > "$OUT/04-normal-evict-R$i.json"
done
"${PY[@]}" --workspace "$NORMAL_ROOT" status > "$OUT/05-normal-status.json"
"${PY[@]}" --workspace "$NORMAL_ROOT" verify > "$OUT/06-normal-verify.json"

echo "[3/8] SIGKILL and recovery"
"${PY[@]}" --workspace "$CRASH_ROOT" load \
  --requests samples/requests.json \
  --config samples/memory_config.json > /dev/null
"${PY[@]}" --workspace "$CRASH_ROOT" merge \
  --package samples/policy_package > /dev/null
set +e
"${PY[@]}" --workspace "$CRASH_ROOT" evict --round-id CRASH \
  --sigkill-after-claims 2 > "$OUT/07-crash-evict.json" 2> "$OUT/07-crash-evict.stderr"
CRASH_EXIT=$?
set -e
echo "$CRASH_EXIT" > "$OUT/07-crash-exit-code.txt"
"${PY[@]}" --workspace "$CRASH_ROOT" status > "$OUT/08-status-before-resume.json"
"${PY[@]}" --workspace "$CRASH_ROOT" resume > "$OUT/09-resume.json"
for i in $(seq 2 14); do
  "${PY[@]}" --workspace "$CRASH_ROOT" evict --round-id "R$i" > /dev/null
done
"${PY[@]}" --workspace "$CRASH_ROOT" status > "$OUT/10-crash-status.json"
"${PY[@]}" --workspace "$CRASH_ROOT" verify > "$OUT/11-crash-verify.json"

echo "[4/8] parallel scheduler/request processes"
"${PY[@]}" --workspace "$PARALLEL_ROOT" load \
  --requests samples/requests.json \
  --config samples/memory_config.json > /dev/null
"${PY[@]}" --workspace "$PARALLEL_ROOT" merge \
  --package samples/policy_package > /dev/null

set +e
"${PY[@]}" --workspace "$PARALLEL_ROOT" evict --round-id MP0 \
  --shard-count 2 --shard-index 0 > "$OUT/12-parallel-mp0.json" &
P0=$!
"${PY[@]}" --workspace "$PARALLEL_ROOT" evict --round-id MP1 \
  --shard-count 2 --shard-index 1 > "$OUT/12-parallel-mp1.json" &
P1=$!
wait "$P0"; C0=$?
wait "$P1"; C1=$?
set -e
printf 'mp0=%s\nmp1=%s\n' "$C0" "$C1" > "$OUT/12-parallel-exit-codes.txt"
for i in $(seq 2 14); do
  "${PY[@]}" --workspace "$PARALLEL_ROOT" evict --round-id "R$i" > /dev/null
done
"${PY[@]}" --workspace "$PARALLEL_ROOT" verify > "$OUT/13-parallel-verify.json"

echo "[5/8] started-write failure classification"
"${PY[@]}" --workspace "$FAILURE_ROOT" load \
  --requests samples/requests.json \
  --config samples/memory_config.json > /dev/null
"${PY[@]}" --workspace "$FAILURE_ROOT" merge \
  --package samples/policy_package > /dev/null
FAIL_ENTRY="$(python3 -c 'import json; print(json.load(open("'$OUT'/04-normal-evict-R1.json"))["evictions"][0]["entry_id"])')"
"${PY[@]}" --workspace "$FAILURE_ROOT" evict --round-id FAIL \
  --fail-entry-id "$FAIL_ENTRY" > "$OUT/13a-started-failure.json"

echo "[6/8] policy atomic replacement, incremental and rollback"
"${PY[@]}" --workspace "$POLICY_ROOT" load \
  --requests samples/requests.json \
  --config samples/memory_config.json > /dev/null
set +e
"${PY[@]}" --workspace "$POLICY_ROOT" merge \
  --package samples/policy_package --sigkill-after-stage \
  > "$OUT/14-policy-kill.json" 2> "$OUT/14-policy-kill.stderr"
POLICY_KILL_EXIT=$?
set -e
echo "$POLICY_KILL_EXIT" > "$OUT/14-policy-kill-exit-code.txt"
"${PY[@]}" --workspace "$POLICY_ROOT" status > "$OUT/15-policy-status-after-kill.json"
"${PY[@]}" --workspace "$POLICY_ROOT" merge \
  --package samples/policy_package > "$OUT/16-policy-merge-v1.json"
"${PY[@]}" --workspace "$POLICY_ROOT" merge \
  --package samples/policy_package_next/v2 > "$OUT/17-policy-merge-v2.json"
"${PY[@]}" --workspace "$POLICY_ROOT" merge \
  --package samples/policy_package_incremental/v2 > "$OUT/18-policy-merge-v3-incremental.json"
set +e
"${PY[@]}" --workspace "$POLICY_ROOT" merge \
  --package samples/policy_package > "$OUT/19-rollback-rejected.json"
ROLLBACK_EXIT=$?
set -e
echo "$ROLLBACK_EXIT" > "$OUT/19-rollback-exit-code.txt"

echo "[7/8] unit tests"
python3 -m unittest discover -s tests -p 'test_*.py' > "$OUT/20-unittest.log" 2>&1

echo "[8/8] determinism summary"
NORMAL_ROOT="$NORMAL_ROOT" CRASH_ROOT="$CRASH_ROOT" PARALLEL_ROOT="$PARALLEL_ROOT" FAILURE_ROOT="$FAILURE_ROOT" OUT="$OUT" python3 - <<'PY'
import json
import os
from pathlib import Path

out = Path(os.environ["OUT"])
normal = json.loads((out / "06-normal-verify.json").read_text())
crash = json.loads((out / "11-crash-verify.json").read_text())
parallel = json.loads((out / "13-parallel-verify.json").read_text())
rollback = json.loads((out / "19-rollback-rejected.json").read_text())
failure = json.loads((out / "13a-started-failure.json").read_text())
summary = {
    "normal_ok": normal["ok"],
    "crash_ok": crash["ok"],
    "parallel_ok": parallel["ok"],
    "normal_ledger_digest": normal["ledger_digest"],
    "crash_ledger_digest": crash["ledger_digest"],
    "parallel_ledger_digest": parallel["ledger_digest"],
    "digests_match": normal["ledger_digest"] == crash["ledger_digest"] == parallel["ledger_digest"],
    "committed_evictions": normal["committed_evictions"],
    "rollback_code": rollback["error"]["code"],
    "started_failure_status": failure["status"],
    "started_failure_counts": failure["counts"],
    "paths": {
        "normal": os.environ["NORMAL_ROOT"],
        "crash": os.environ["CRASH_ROOT"],
        "parallel": os.environ["PARALLEL_ROOT"],
        "failure": os.environ["FAILURE_ROOT"],
    },
}
(out / "21-validation-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
if not summary["digests_match"] or not (normal["ok"] and crash["ok"] and parallel["ok"]):
    raise SystemExit(1)
if summary["started_failure_counts"].get("failed_started") != 1:
    raise SystemExit("failed_started injection was not observed")
PY

echo "Validation artifacts: $OUT"
