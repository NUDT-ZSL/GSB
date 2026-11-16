param(
    [string]$Output = "bin\\middleware.exe"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
go version
python --version
python -c "import tkinter; print('tkinter', tkinter.TkVersion)"
New-Item -ItemType Directory -Force (Split-Path -Parent $Output) | Out-Null
go build -trimpath -o $Output .\cmd\middleware
Write-Host "Built $Output"
