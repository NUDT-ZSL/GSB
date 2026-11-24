[CmdletBinding()]
param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $RepoRoot

try {
    Write-Host "== Python version =="
    & $Python --version
    if ($LASTEXITCODE -ne 0) { throw "Python 3.11+ is required" }

    Write-Host "== pytest =="
    & $Python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "pytest failed" }

    $RunDir = Join-Path ([System.IO.Path]::GetTempPath()) ("memguard-" + [System.Guid]::NewGuid().ToString("N"))
    $Sample = Join-Path $RunDir "sample"
    $State = Join-Path $RunDir "state"
    New-Item -ItemType Directory -Path $RunDir | Out-Null

    Write-Host "== deterministic sample =="
    & $Python -m memguard generate-sample --out $Sample
    if ($LASTEXITCODE -ne 0) { throw "sample generation failed" }

    Write-Host "== process (planted invalid records make partial exit 1) =="
    & $Python -m memguard process --sample-dir $Sample --state-dir $State
    if ($LASTEXITCODE -ne 1) { throw "expected partial-success exit code 1" }

    Write-Host "== full replay checkpoint verification =="
    & $Python -m memguard verify-checkpoint --state-dir $State --full-replay
    if ($LASTEXITCODE -ne 0) { throw "checkpoint verification failed" }

    Write-Host "== lock conflict =="
    $Holder = Start-Process -FilePath $Python `
        -ArgumentList @("-m", "memguard", "lock-hold", "--state-dir", $State, "--seconds", "5") `
        -PassThru -NoNewWindow
    Start-Sleep -Milliseconds 700
    & $Python -m memguard process --state-dir $State | Out-Null
    $LockExit = $LASTEXITCODE
    $Holder.WaitForExit()
    if ($LockExit -ne 3) { throw "expected E_LOCK exit code 3, got $LockExit" }

    Write-Host "VALIDATION_OK"
}
finally {
    Pop-Location
}
