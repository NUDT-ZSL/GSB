$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$env:PYTHONPATH = Join-Path $Root "src"
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    $Python = "python"
}
& $Python -m qrw demo --output artifacts\demo_report.json @args
exit $LASTEXITCODE

