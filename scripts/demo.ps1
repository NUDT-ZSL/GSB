param(
    [string]$DataDir = ".runtime\\demo-data",
    [string]$ArtifactsDir = "artifacts\\demo"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
python tools\\demo.py --data-dir $DataDir --artifacts-dir $ArtifactsDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Host ""
Write-Host "Open dashboard with:"
Write-Host "python tools\\gui.py --data-dir $DataDir"
