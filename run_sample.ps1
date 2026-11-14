# Generate the deterministic samples, export them, and open the main preview.
# Usage:  .\run_sample.ps1            (generate + export + open preview)
#         .\run_sample.ps1 -NoOpen    (generate + export only)
param(
    [switch]$NoOpen,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

Write-Host "==> Generating deterministic samples" -ForegroundColor Cyan
& $Python tools/make_sample.py

Write-Host "==> Exporting clean sample" -ForegroundColor Cyan
& $Python -m frame_export samples/clean_storyboard/description.json exports/clean
$cleanExit = $LASTEXITCODE

Write-Host "==> Exporting issues sample" -ForegroundColor Cyan
& $Python -m frame_export samples/storyboard_issues/description.json exports/issues
$issuesExit = $LASTEXITCODE

Write-Host "==> Exporting invalid-fps sample (expected to fail readably)" -ForegroundColor Cyan
& $Python -m frame_export samples/invalid_fps/description.json exports/invalid_fps
$invalidExit = $LASTEXITCODE

Write-Host ""
Write-Host "Exit codes: clean=$cleanExit (expect 0), issues=$issuesExit (expect 2), invalid_fps=$invalidExit (expect 3)"
Write-Host "Preview:    exports/clean/preview.html"
Write-Host "Report:     exports/clean/report.json"

if (-not $NoOpen -and (Test-Path "exports/clean/preview.html")) {
    Start-Process "exports/clean/preview.html"
}
