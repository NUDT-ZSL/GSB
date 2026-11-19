param(
    [string]$Python = "py",
    [string]$WheelDir = "wheels"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".venv")) {
    if ($Python -eq "py") {
        & py -3.11 -m venv .venv
    } else {
        & $Python -m venv .venv
    }
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
$LocalWheels = Join-Path $Root $WheelDir
$Wheel = Get-ChildItem -Path $LocalWheels -Filter "numpy*.whl" -ErrorAction SilentlyContinue |
    Select-Object -First 1

if ($Wheel) {
    & $VenvPython -m pip install --no-index --find-links $LocalWheels numpy
} else {
    Write-Host "No local NumPy wheel found in $LocalWheels; using configured package index."
    & $VenvPython -m pip install -r requirements.txt
}

& $VenvPython -m pip install -e . --no-deps
Write-Host "Setup complete. Activate with: .\.venv\Scripts\Activate.ps1"

