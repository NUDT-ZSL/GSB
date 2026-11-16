param(
    [Parameter(Mandatory = $true)][string]$PartitionKey,
    [Parameter(Mandatory = $true)][string]$Session,
    [string]$Address = "127.0.0.1:19160",
    [int]$Limit = 10,
    [int]$WindowTimeoutMs = 5000,
    [string]$FailurePlan = "data/no-failures.json",
    [string]$MarkerOut = "output/consumer-markers.jsonl",
    [string]$Report = "output/consumer-report.json",
    [long]$TargetOffset = 0,
    [switch]$PullOnly
)
$ErrorActionPreference = "Stop"
$binary = Join-Path $PSScriptRoot "..\output\bin\orderqueue.exe"
$consumerArgs = @(
    "consume",
    "--addr", $Address,
    "--partition-key", $PartitionKey,
    "--session", $Session,
    "--limit", $Limit,
    "--window-timeout-ms", $WindowTimeoutMs,
    "--failure-plan", $FailurePlan,
    "--marker-out", $MarkerOut,
    "--report", $Report,
    "--target-offset", $TargetOffset
)
if ($PullOnly) { $consumerArgs += "--pull-only" }
& $binary @consumerArgs
exit $LASTEXITCODE
