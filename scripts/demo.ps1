param(
    [string]$Bin = "",
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Bin) {
    $Bin = if ($env:BIN) { $env:BIN } else { Join-Path $Root "target\debug\logstore.exe" }
}
if (-not $Out) { $Out = Join-Path $Root "demo-output" }
$Samples = Join-Path $Root "samples\basic"

if (-not (Test-Path $Bin)) { throw "binary not found: $Bin; run cargo build first" }
if (-not (Test-Path (Join-Path $Samples "session.json"))) {
    & $Bin generate-samples (Join-Path $Root "samples")
}
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Path $Out | Out-Null

Write-Host "=== scenario 1: first recovery from snapshot ==="
$s1 = Join-Path $Out "scenario1"
New-Item -ItemType Directory -Path $s1 | Out-Null
Copy-Item -Recurse (Join-Path $Samples "data") (Join-Path $s1 "data")
@("views", "trace", "quit") |
    & $Bin --data-dir (Join-Path $s1 "data") --session (Join-Path $Samples "session.json") --auto-recover |
    Tee-Object -FilePath (Join-Path $Out "scenario1.txt")

Write-Host ""
Write-Host "=== scenario 2: incremental write, snapshot point, validation ==="
$s2 = Join-Path $Out "scenario2"
New-Item -ItemType Directory -Path $s2 | Out-Null
Copy-Item -Recurse (Join-Path $Samples "data") (Join-Path $s2 "data")
$session2 = Join-Path $s2 "session.json"
@(
    "put user:cn:carol Carol",
    "snapshot",
    "snapshot-save $session2",
    "validate",
    "segments",
    "views",
    "trace",
    "quit"
) | & $Bin --data-dir (Join-Path $s2 "data") --session (Join-Path $Samples "session.json") --auto-recover |
    Tee-Object -FilePath (Join-Path $Out "scenario2.txt")

Write-Host ""
Write-Host "=== scenario 3: injected tail truncation and recovery ==="
$s3 = Join-Path $Out "scenario3"
New-Item -ItemType Directory -Path $s3 | Out-Null
Copy-Item -Recurse (Join-Path $Samples "data") (Join-Path $s3 "data")
@("views", "trace", "quit") |
    & $Bin --data-dir (Join-Path $s3 "data") --session (Join-Path $Samples "session.json") --auto-recover --inject-tail-truncate "000002.log:8" |
    Tee-Object -FilePath (Join-Path $Out "scenario3.txt")

Write-Host ""
Write-Host "demo output written to $Out"
