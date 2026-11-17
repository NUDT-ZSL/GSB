$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

Write-Host "Python:"
py -3.11 --version

py -3.11 -m cluster_ledger validate --config data/clusters.json --sample-data data/sample_data.json
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

py -3.11 -m cluster_ledger demo --workdir var/demo
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Report: var/demo/report.json"
Write-Host "Structured log: var/demo/events.jsonl"
