@echo off
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)
"%PYTHON%" -m replica_ledger.cli port-check --ports 19101,19102,19103,19104,19105,19106
endlocal
