# Build Windows binaries from a Windows host (Go 1.21+ required).
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
New-Item -ItemType Directory -Force -Path bin | Out-Null
go build -o bin/lockd.exe ./cmd/lockd
go build -o bin/lockctl.exe ./cmd/lockctl
Write-Host "built bin/lockd.exe bin/lockctl.exe"
