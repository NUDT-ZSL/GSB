$ErrorActionPreference = "Stop"
$root = (Convert-Path (Join-Path $PSScriptRoot ".."))
Set-Location $root
$binary = Join-Path $root "output\bin\orderqueue.exe"
if (Get-Command go -ErrorAction SilentlyContinue) {
    & (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
} elseif (Test-Path $binary) {
    Write-Output "go is not on PATH; using prebuilt $binary"
} else {
    throw "go is not on PATH and output\bin\orderqueue.exe is missing"
}
& (Join-Path $PSScriptRoot "demo-normal.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot "demo-failure.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot "demo-recovery.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& (Join-Path $PSScriptRoot "demo-faults.ps1")
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "all scenarios passed"
