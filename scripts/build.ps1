$ErrorActionPreference = "Stop"
$root = (Convert-Path (Join-Path $PSScriptRoot ".."))
Set-Location $root
if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    throw "go is not on PATH; install Go 1.21+ or add it to PATH"
}
$env:CGO_ENABLED = "0"
New-Item -ItemType Directory -Force -Path (Join-Path $root "output\bin") | Out-Null
& go build -mod=vendor -trimpath -o output\bin\orderqueue.exe .\cmd\orderqueue
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
Write-Output "built output\bin\orderqueue.exe (verify and fault are built in)"
