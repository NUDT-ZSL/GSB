$ErrorActionPreference = "Stop"

Write-Host "[1/5] Python version"
python --version

Write-Host "[2/5] Installing local verification dependencies"
python -m pip install numpy pytest

$Work = Join-Path $env:TEMP ("supply-chain-audit-" + [guid]::NewGuid().ToString("N"))
$Sample = Join-Path $Work "seed"
$Workspace = Join-Path $Work "checkpoint"
New-Item -ItemType Directory -Force -Path $Work | Out-Null

Write-Host "[3/5] Generating deterministic sample at $Sample"
python .\generate_seed_samples.py $Sample

Write-Host "[4/5] Running pytest"
python -m pytest -q

Write-Host "[5/5] Running CLI acceptance workflow"
python -m supply_chain_audit import-snapshots --sample-dir $Sample --workspace $Workspace --concurrency 4 --timeout-seconds 10 --queue-limit 32 --max-hash-operations 10000 --apply-seed-updates --report (Join-Path $Work "import.json") | Out-Null
python -m supply_chain_audit query-view --workspace $Workspace --status revoked --out (Join-Path $Work "query.json") | Out-Null
python -m supply_chain_audit export-closure --workspace $Workspace --out (Join-Path $Work "closure.json") | Out-Null
python -m supply_chain_audit verify-checkpoint --workspace $Workspace

Write-Host "Validation artifacts: $Work"
