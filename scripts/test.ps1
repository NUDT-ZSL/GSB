$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
go test ./...
python -m unittest discover -s tests -v
python tools/demo.py
python tools\gui.py --data-dir .runtime\demo-data --check
python tools/verify.py --data-dir .runtime\demo-data --output artifacts\demo\verification.json
