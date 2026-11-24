# Windows PowerShell validation for Python 3.11 and the standard library only.
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

if (Get-Command py -ErrorAction SilentlyContinue) {
    $Python = "py"
    $Prefix = @("-3.11")
} else {
    $Python = "python"
    $Prefix = @()
}

function Invoke-ExpectedExit {
    param(
        [Parameter(Mandatory = $true)][int]$Expected,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Python @Prefix @Arguments
    $Actual = $LASTEXITCODE
    if ($Actual -ne $Expected) {
        throw "Expected exit code $Expected but got $Actual for: $Arguments"
    }
    Write-Host "OK expected_exit=$Expected actual_exit=$Actual"
}

Write-Host "## 1. Generate fixed-seed samples"
Invoke-ExpectedExit 0 @("-m", "screen_diag", "generate", "--out", "samples")

Write-Host "## 2. Unit tests"
Invoke-ExpectedExit 0 @("-m", "unittest", "discover", "-s", "tests", "-v")

Write-Host "## 3. Baseline"
Invoke-ExpectedExit 0 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples",
    "--report", "reports/baseline.json",
    "--state-dir", "state/validate-baseline",
    "--workers", "4",
    "--memory-budget-bytes", "65536"
)

Write-Host "## 4. Corrupt digest"
Invoke-ExpectedExit 3 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples", "--inject", "corrupt_digest",
    "--inject-workdir", "out/validation-injected",
    "--report", "reports/validate-corrupt-digest.json",
    "--state-dir", "state/validate-corrupt-digest"
)

Write-Host "## 5. Screen count mismatch"
Invoke-ExpectedExit 3 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples", "--inject", "screen_count_mismatch",
    "--inject-workdir", "out/validation-injected",
    "--report", "reports/validate-count-mismatch.json",
    "--state-dir", "state/validate-count-mismatch"
)

Write-Host "## 6. Out-of-bounds time"
Invoke-ExpectedExit 2 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples", "--inject", "time_out_of_bounds",
    "--inject-workdir", "out/validation-injected",
    "--report", "reports/validate-oob.json"
)

Write-Host "## 7. Replay drift"
Invoke-ExpectedExit 4 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples", "--inject", "replay_drift",
    "--inject-workdir", "out/validation-injected",
    "--report", "reports/validate-replay-drift.json",
    "--state-dir", "state/validate-replay-drift"
)

Write-Host "## 8. Interrupt and resume"
Invoke-ExpectedExit 130 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples",
    "--state-dir", "state/validate-interrupt",
    "--report", "reports/validate-interrupted.json",
    "--interrupt-after-chunks", "2"
)
Invoke-ExpectedExit 0 @(
    "-m", "screen_diag", "analyze",
    "--samples", "samples",
    "--state-dir", "state/validate-interrupt",
    "--report", "reports/validate-resumed.json",
    "--resume"
)
Invoke-ExpectedExit 0 @("-m", "screen_diag", "recover", "--samples", "samples", "--state-dir", "state/validate-interrupt")

Write-Host "VALIDATION_OK"
