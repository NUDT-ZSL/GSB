$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    throw "go command not found; install Go 1.21+ and add it to PATH"
}

go test ./...
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
go vet ./...
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$CCompiler = if ($env:CC) { $env:CC } else { "gcc" }
if (Get-Command $CCompiler -ErrorAction SilentlyContinue) {
    $env:CGO_ENABLED = "1"
    go test -race ./...
    exit $LASTEXITCODE
}
Write-Warning "race test skipped: CGO C compiler not found (set CC to a working C compiler)"
exit 0
