param(
    [string]$Out = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Bin = if ($env:BIN) { $env:BIN } else { Join-Path $Root "target\debug\logstore.exe" }
if (-not $Out) { $Out = Join-Path $Root "samples" }

if (-not (Test-Path $Bin)) {
    throw "binary not found: $Bin; run cargo build first"
}

& $Bin generate-samples $Out
exit $LASTEXITCODE
