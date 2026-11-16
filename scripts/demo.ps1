$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

if (-not (Get-Command go -ErrorAction SilentlyContinue)) {
    throw "go command not found; install Go 1.21+ and add it to PATH"
}

New-Item -ItemType Directory -Force -Path bin | Out-Null
go build -o bin\localdns.exe ./cmd/localdns
go build -o bin\dnsupstream.exe ./cmd/dnsupstream
go build -o bin\dnsdemo.exe ./cmd/dnsdemo

& .\bin\dnsdemo.exe `
    -zone samples\zone.json `
    -wire samples\answer.wire `
    -wire-query samples\query.wire `
    -upstream-exe .\bin\dnsupstream.exe
exit $LASTEXITCODE

