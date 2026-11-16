param(
    [Parameter(Mandatory = $true)][string]$PartitionKey,
    [Parameter(Mandatory = $true)][long]$Offset,
    [Parameter(Mandatory = $true)][string]$Result,
    [string]$MarkerOut = "output/consumer-markers.jsonl"
)
$parent = Split-Path -Parent $MarkerOut
if ($parent) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
$record = [ordered]@{
    ts = (Get-Date).ToUniversalTime().ToString("o")
    partition_key = $PartitionKey
    offset = $Offset
    result = $Result
} | ConvertTo-Json -Compress
Add-Content -Path $MarkerOut -Value $record -Encoding utf8
