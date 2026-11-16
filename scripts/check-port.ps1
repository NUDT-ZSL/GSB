param([int]$Port = 19160)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "common.ps1")
if (Test-TcpPortInUse $Port) {
    Write-Error "TCP port $Port is already in use"
    exit 1
}
Write-Output "port $Port is available"
