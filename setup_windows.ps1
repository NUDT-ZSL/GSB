$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Get-Command py -ErrorAction SilentlyContinue
if ($null -eq $python) {
    throw "Python Launcher (py.exe) was not found. Install Python 3.11 from python.org first."
}

& py -3.11 --version
& py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m pip install -e .
& .\.venv\Scripts\python.exe -m tactical_engine generate-sample

Write-Host "Setup complete. Launch run_cli.bat or run_gui.bat."
