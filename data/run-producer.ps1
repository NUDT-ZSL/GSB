param(
    [string]$Address = "127.0.0.1:19160",
    [string]$InputPath = "data/producer.jsonl",
    [string]$Report = "output/producer-report.json"
)
$ErrorActionPreference = "Stop"
$binary = Join-Path $PSScriptRoot "..\output\bin\orderqueue.exe"
& $binary produce --addr $Address --input $InputPath --report $Report
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
