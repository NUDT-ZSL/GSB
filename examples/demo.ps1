# Windows PowerShell diagnostic workbench demo.
# Run from the project root after installing Python 3.11.

$ErrorActionPreference = "Stop"

python -m pip install pytest==8.2.0
python -m sandbox_guard generate-sample --seed 361 --out samples/generated
python -m pytest -q

$work = ".sandbox-guard-demo"
python -m sandbox_guard import-patches `
  --work-dir $work `
  --policy samples/generated/policy.json `
  samples/generated/patches/patch-100-revoke-root.json `
  samples/generated/patches/patch-200-reduce-and-dependency.json `
  samples/generated/patches/patch-300-revive-tombstone.json `
  samples/generated/patches/patch-400-revive-restore.json `
  --report artifacts/import.json

python -m sandbox_guard query-process 4103 --work-dir $work --report artifacts/query-4103.json
python -m sandbox_guard export-closure --work-dir $work --report artifacts/closure.json
python -m sandbox_guard verify-checkpoint --work-dir $work --report artifacts/verify.json

# Partial-failure / atomic batch demonstration returns exit code 1:
python -m sandbox_guard import-patches `
  --work-dir $work `
  samples/generated/invalid/bad-batch.json `
  --report artifacts/invalid-import.json

# Interactive workbench:
# python -m sandbox_guard shell --work-dir $work
