param(
    [string]$PythonLauncher = "py"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Venv = Join-Path $Root ".venv"
if (-not (Test-Path $Venv)) {
    if ($PythonLauncher -eq "py") {
        & py -3.11 -m venv $Venv
    } else {
        & $PythonLauncher -m venv $Venv
    }
}
$Python = Join-Path $Venv "Scripts\python.exe"
& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $Root "requirements.txt")
& $Python -m unittest discover -s (Join-Path $Root "tests") -v
Write-Host ""
Write-Host "Environment ready."
Write-Host "CLI: $Python $(Join-Path $Root 'run_cli.py') acceptance"
Write-Host "GUI: $Python $(Join-Path $Root 'run_gui.py')"
