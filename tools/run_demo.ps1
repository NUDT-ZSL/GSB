param(
    [string]$OutputDir = "demo"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot

if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    throw "ffmpeg 不在 PATH 中"
}
if (-not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    throw "ffprobe 不在 PATH 中"
}

Push-Location $RepoRoot
try {
    python tools\generate_samples.py --output-dir $OutputDir
    python transcoder.py (Join-Path $OutputDir "manifest.json")
}
finally {
    Pop-Location
}
