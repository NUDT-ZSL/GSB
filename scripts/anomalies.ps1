param(
    [string]$Bin = "",
    [string]$Out = ""
)

$Root = Split-Path -Parent $PSScriptRoot
if (-not $Bin) {
    $Bin = if ($env:BIN) { $env:BIN } else { Join-Path $Root "target\debug\logstore.exe" }
}
if (-not $Out) { $Out = Join-Path $Root "demo-output\anomalies" }
$Samples = Join-Path $Root "samples\basic"
if (-not (Test-Path $Bin)) { throw "binary not found: $Bin; run cargo build first" }
if (-not (Test-Path (Join-Path $Samples "session.json"))) {
    & $Bin generate-samples (Join-Path $Root "samples") | Out-Null
}
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Path $Out | Out-Null

function Copy-SampleData([string]$Name) {
    $target = Join-Path $Out $Name
    New-Item -ItemType Directory -Path (Join-Path $target "data") -Force | Out-Null
    Copy-Item -Recurse (Join-Path $Samples "data\*") (Join-Path $target "data")
    return $target
}

Write-Host "=== E_LOG: injected tail truncation repaired during recovery ==="
$dir = Copy-SampleData "log"
@("quit") | & $Bin --data-dir (Join-Path $dir "data") --session (Join-Path $Samples "session.json") --auto-recover --inject-tail-truncate "000002.log:8" | Tee-Object -FilePath (Join-Path $Out "log.txt")
$code = $LASTEXITCODE
Write-Host "exit_code=$code"

Write-Host ""
Write-Host "=== E_CORRUPT: checksum damage in a non-final segment ==="
$dir = Copy-SampleData "corrupt"
@("quit") | & $Bin --data-dir (Join-Path $dir "data") --inject-corrupt-checksum "000001.log:1" --auto-recover | Tee-Object -FilePath (Join-Path $Out "corrupt.txt")
$code = $LASTEXITCODE
Write-Host "exit_code=$code"

Write-Host ""
Write-Host "=== E_DEPEND: cycle injected before graph load ==="
$dir = Copy-SampleData "depend"
@("quit") | & $Bin --data-dir (Join-Path $dir "data") --graph (Join-Path $Samples "graph.json") --inject-dependency-cycle --auto-recover | Tee-Object -FilePath (Join-Path $Out "depend.txt")
$code = $LASTEXITCODE
Write-Host "exit_code=$code"

Write-Host ""
Write-Host "=== E_SNAPSHOT: snapshot-referenced segment missing ==="
$dir = Copy-SampleData "snapshot"
@("quit") | & $Bin --data-dir (Join-Path $dir "data") --session (Join-Path $Samples "session.json") --inject-missing-snapshot-segment "000001.log" | Tee-Object -FilePath (Join-Path $Out "snapshot.txt")
$code = $LASTEXITCODE
Write-Host "exit_code=$code"

Write-Host ""
Write-Host "anomaly output written to $Out"
