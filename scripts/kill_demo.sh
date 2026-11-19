#!/usr/bin/env bash
# Acceptance demo: hard-kill recovery must be byte-identical to a clean run.
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "== generate deterministic samples =="
$PY scripts/gen_samples.py --data data --resources resources --pairs 120 --seed 20261120

rm -rf state_crash state_clean

echo "== run 1: hard kill during score stage (pair 40) =="
set +e
XLPIPE_KILL_AT=score:40 $PY scripts/run_pipeline.py --state state_crash
echo "killed run exit code: $?"
set -e

echo "== run 2: resume from last committed snapshot =="
$PY scripts/run_pipeline.py --state state_crash

echo "== reference: uninterrupted clean run =="
$PY scripts/run_pipeline.py --state state_clean

echo "== byte-identity check on final ranking =="
for stage in normalize align score summarize; do
  diff "state_crash/stages/$stage/v001.jsonl" "state_clean/stages/$stage/v001.jsonl" \
    && echo "stage $stage: BYTE-IDENTICAL"
done

echo "== independent verification of recovered state =="
$PY scripts/verify.py --state state_crash
