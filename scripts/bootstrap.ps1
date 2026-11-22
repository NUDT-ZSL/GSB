$ErrorActionPreference = "Stop"

if (-not (Test-Path ".venv")) {
    py -3.12 -m venv .venv
}

$python = Join-Path $PWD ".venv\Scripts\python.exe"
& $python -m pip install --upgrade pip
& $python -m pip install -r requirements.txt
& $python scripts\generate_samples.py samples
& $python scripts\validate_samples.py samples
& $python -m pytest -q

Write-Host "Bootstrap complete. Run: .\.venv\Scripts\python -m h2diag demo"
