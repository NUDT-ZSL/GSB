#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m pip install -r requirements.txt
python3 -m narrative_engine.cli generate-sample --out samples --seed 351
python3 -m narrative_engine.cli validate --script samples/story.json --choices samples/choice_sequence.json
python3 -m narrative_engine.cli run \
  --script samples/story.json \
  --choices samples/choice_sequence.json \
  --state-dir runtime/state \
  --report runtime/run_report.json
python3 -m narrative_engine.cli explore \
  --script samples/story.json \
  --report runtime/branch_report.json
python3 -m narrative_engine.cli explore \
  --script samples/story.json \
  --workers 4 \
  --choice-sequences samples/choice_sequences.json \
  --report runtime/sequence_precompute_report.json
python3 -m unittest discover -s tests

echo "Demo complete. See runtime/run_report.json, runtime/branch_report.json, and runtime/sequence_precompute_report.json"
