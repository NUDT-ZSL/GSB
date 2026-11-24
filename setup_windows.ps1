$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if (-not $pythonLauncher) {
    throw "Python launcher 'py' was not found. Install Python 3.11 and enable the py launcher."
}

py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe -m voice_repair generate --data-dir data --seed 20261125 --force
& .\.venv\Scripts\python.exe -m voice_repair validate --task data\task.json

Write-Host "Environment ready. Run .\run_batch.bat or .\verify.bat"
