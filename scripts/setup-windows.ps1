# Run in PowerShell from the repository root.
$ErrorActionPreference = "Stop"

py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\pip.exe install -r requirements.txt
.\.venv\Scripts\python.exe -m pdre.cli generate-sample --out samples\demo

Write-Host "Setup complete. Run:"
Write-Host "  .\.venv\Scripts\python.exe -m pytest -q"
Write-Host "  .\.venv\Scripts\python.exe -m pdre.cli run --input-dir samples\demo --report reports\report.json --checkpoint-dir state\checkpoints --snapshot-dir state\snapshots --ledger state\ledger.jsonl"
