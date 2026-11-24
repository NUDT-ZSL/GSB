$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)
python -m pip install -r requirements.txt
python -m narrative_engine.cli generate-sample --out samples --seed 351
python -m narrative_engine.cli validate --script samples/story.json --choices samples/choice_sequence.json
python -m narrative_engine.cli run `
  --script samples/story.json `
  --choices samples/choice_sequence.json `
  --state-dir runtime/state `
  --report runtime/run_report.json
python -m narrative_engine.cli explore `
  --script samples/story.json `
  --report runtime/branch_report.json
python -m narrative_engine.cli explore `
  --script samples/story.json `
  --choice-sequences samples/choice_sequences.json `
  --workers 4 `
  --report runtime/sequence_precompute_report.json
python -m unittest discover -s tests

Write-Host "Demo complete. See runtime/run_report.json, runtime/branch_report.json, and runtime/sequence_precompute_report.json"
