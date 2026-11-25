#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p validation state
export PYTHONPATH="$ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

python3 scripts/generate_samples.py > validation/seed.json
python3 -m pytest tests > validation/pytest.txt 2>&1

MAIN_DB="state/acceptance.db"
CRASH_DB="state/acceptance-crash.db"
MAIN_CLI=(python3 -m tierflow.cli --db "$MAIN_DB")
CRASH_CLI=(python3 -m tierflow.cli --db "$CRASH_DB")

"${MAIN_CLI[@]}" load --reset > validation/load.json
"${MAIN_CLI[@]}" merge > validation/merge-full.json
"${MAIN_CLI[@]}" merge > validation/merge-noop.json
"${MAIN_CLI[@]}" judge > validation/judge.json
"${MAIN_CLI[@]}" migrate --round-id acceptance > validation/migrate.json
"${MAIN_CLI[@]}" status --round-id acceptance > validation/status.json
"${MAIN_CLI[@]}" verify > validation/verify.json

"${MAIN_CLI[@]}" merge --update-package samples/policy_packages/append-v11.json > validation/policy-append.json
"${MAIN_CLI[@]}" merge --update-package samples/policy_packages/append-v11.json > validation/policy-append-noop.json
set +e
"${MAIN_CLI[@]}" merge --update-package samples/policy_packages/rollback-v9.json > validation/policy-rollback.json
ROLLBACK_RC=$?
"${MAIN_CLI[@]}" --clock 1700000100000 judge > validation/clock-rollback.json
CLOCK_RC=$?
set -e
echo "policy_rollback_exit=${ROLLBACK_RC}" > validation/exit-codes.txt
echo "clock_rollback_exit=${CLOCK_RC}" >> validation/exit-codes.txt

"${CRASH_CLI[@]}" load --reset > validation/crash-load.json
"${CRASH_CLI[@]}" merge > validation/crash-merge.json
set +e
"${CRASH_CLI[@]}" migrate --round-id crash --crash-after-claim obj-0006 > validation/crash-killed.json 2> validation/crash-killed.stderr
CRASH_RC=$?
set -e
echo "crash_exit=${CRASH_RC}" > validation/crash-result.txt
"${CRASH_CLI[@]}" resume --round-id crash > validation/resume.json
"${CRASH_CLI[@]}" verify > validation/crash-verify.json

python3 - <<'PY' > validation/summary.txt
import json
from pathlib import Path

def load(name):
    return json.loads(Path("validation", name).read_text())

migrate = load("migrate.json")
verify = load("verify.json")
resume = load("resume.json")
merge = load("merge-full.json")
noop = load("merge-noop.json")
append = load("policy-append.json")
rollback = load("policy-rollback.json")
clock = load("clock-rollback.json")
exit_codes = Path("validation/exit-codes.txt").read_text().strip().replace("\n", "; ")
print("acceptance_ok=true")
print(exit_codes)
print("merge_decision=" + merge["apply"]["decision"])
print("merge_noop=" + noop["apply"]["decision"])
print("append_decision=" + append["apply"]["decision"])
print("rollback_code=" + rollback["code"])
print("clock_code=" + clock["code"])
print("migration_status_counts=" + json.dumps(migrate["status_counts"], sort_keys=True))
print("migration_outcome_counts=" + json.dumps(migrate["outcome_counts"], sort_keys=True))
pair = migrate["precision_pairs"][0]
print("precision_pair=" + pair["object_a"] + "/" + pair["object_b"])
print("precision_cost_difference_scaled=" + str(pair["cost_difference_scaled"]))
print("precision_tolerance_scaled=" + str(pair["tolerance_scaled"]))
print("verify_ok=" + str(verify["ok"]).lower())
print("resume_recovered=" + str(len(resume["recovered_active_jobs"])))
print("resume_status_counts=" + json.dumps(resume["report"]["status_counts"], sort_keys=True))
print("resume_watermark=" + str(resume["report"]["watermarks"]["migration_committed"]))
PY

cat validation/summary.txt
