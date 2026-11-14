# Deterministic demo for the two upload-service defects (PowerShell 7+).
# Usage:
#   1. start a service in another terminal:
#        python vuln_server.py            (port 8001)
#        python fixed_server.py           (port 8002)
#   2. run:  ./demo.ps1 vuln     or     ./demo.ps1 fixed
param(
    [Parameter(Mandatory=$true)]
    [ValidateSet("vuln", "fixed")]
    [string]$Target
)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if ($Target -eq "vuln") {
    $port = 8001
    $workdir = "vuln_workdir"
} else {
    $port = 8002
    $workdir = "fixed_workdir"
}

Write-Host "== detector against $Target service (port $port) ==" -ForegroundColor Cyan
python detect.py --target $Target --url "http://127.0.0.1:$port" --base-dir $workdir

Write-Host "`n== on-disk state of $workdir ==" -ForegroundColor Cyan
if (Test-Path $workdir) {
    Get-ChildItem -Recurse -Force $workdir |
        Select-Object FullName, Length |
        Format-Table -AutoSize
} else {
    Write-Host "(workdir not created yet)"
}
