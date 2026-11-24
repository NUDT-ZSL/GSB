[CmdletBinding()]
param(
    [string]$Python = "python",
    [switch]$InstallDeps
)

$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if ($InstallDeps) {
    & $Python -m pip install -r requirements-dev.txt
    & $Python -m pip install -e .
}

& $Python scripts/generate_samples.py
& $Python -m pytest
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Remove-Item -Recurse -Force state-demo -ErrorAction SilentlyContinue
& $Python -m offline_authz import --input-dir samples/generated --state-dir state-demo
& $Python -m offline_authz adjudicate --input-dir samples/generated --state-dir state-demo --watermark 1
& $Python -m offline_authz adjudicate --input-dir samples/generated --state-dir state-demo --watermark 2
& $Python -m offline_authz revoke --input-dir samples/generated --state-dir state-demo --watermark 2
& $Python -m offline_authz report --state-dir state-demo --output state-demo/diagnostic-report.json
& $Python -m offline_authz verify-checkpoint --state-dir state-demo
& $Python scripts/verify_determinism.py
