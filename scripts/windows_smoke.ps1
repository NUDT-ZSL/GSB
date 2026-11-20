$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$env:PYTHONUTF8 = "1"

python --version
python -c "import numpy; print('NumPy', numpy.__version__)"

New-Item -ItemType Directory -Force -Path "reports" | Out-Null
python -m rope_attention.cli --root . prepare
python -m pytest -q
python scripts/demo_process_pool.py | Set-Content -Encoding utf8 reports/windows_process_pool.json
python scripts/demo_kill_restart.py | Set-Content -Encoding utf8 reports/windows_kill_restart.json
python -m rope_attention.cli --root . demo --output reports/windows_demo.json
python -m rope_attention.cli --root . fault-demo --output reports/windows_fault_demo.json

Write-Host "Windows smoke test complete."

