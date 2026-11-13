# Game audio batch-pipeline demo (Windows PowerShell 5.1+)
# Usage: run  ./demo.ps1  from the project root.
# Python runs in isolated mode (-I); standard library only, no network, no containers.

# Avoid 'Stop': native-command stderr is wrapped into a terminating error in PS 5.1.
# Each step checks $global:LASTEXITCODE explicitly.
$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

$Root = $PSScriptRoot
Set-Location $Root

function Invoke-Step([string]$Cmd) {
    Invoke-Expression $Cmd
    if ($global:LASTEXITCODE -ne 0) {
        Write-Output "STEP FAILED (exit $global:LASTEXITCODE): $Cmd"
        exit $global:LASTEXITCODE
    }
}

Write-Output "=== Step 1/5: generate deterministic fixtures (valid / 8bit / text / corrupt / 96kHz WAV) ==="
Invoke-Step "python -I tests/make_fixtures.py --out testdata/input"

Write-Output ""
Write-Output "=== Step 2/5: batch with full config (+6dB gain / -45dB threshold / 100ms fade) ==="
Invoke-Step "python -I process_audio.py --input testdata/input --output testdata/output_full --config configs/full.json --report testdata/output_full/report.json"

Write-Output ""
Write-Output "=== Step 3/5: batch with empty config (defaults; provides no-fade reference) ==="
Invoke-Step "python -I process_audio.py --input testdata/input --output testdata/output_empty --config configs/empty.json"

Write-Output ""
Write-Output "=== Step 4/5: independent numeric acceptance checks (criteria 1/2/3/4/6/10) ==="
Invoke-Step "python -I verify_outputs.py"

Write-Output ""
Write-Output "=== Step 5/5: illegal config must be rejected (+40dB; expect exit code 2 and no output) ==="
python -I process_audio.py --input testdata/input --output testdata/output_should_not_exist --config configs/bad_gain.json
$badCode = $global:LASTEXITCODE
Write-Output "illegal-config exit code: $badCode (2 means config error)"
$outCreated = Test-Path testdata/output_should_not_exist
Write-Output "output directory created: $outCreated (expected False)"
if ($badCode -ne 2 -or $outCreated) {
    Write-Output "ILLEGAL CONFIG GUARD DID NOT BEHAVE AS EXPECTED"
    exit 1
}

Write-Output ""
Write-Output "ALL DEMO STEPS FINISHED. Unit tests: python -I run_tests.py"
# The last native command intentionally exited 2 (illegal config); the demo itself succeeded.
exit 0
