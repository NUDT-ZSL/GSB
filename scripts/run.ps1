param(
    [string]$Addr = "127.0.0.1:19090",
    [string]$DataDir = ".runtime\\data",
    [string]$RetryAfter = "1s"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
go run .\cmd\middleware -addr $Addr -data $DataDir -retry-after $RetryAfter
