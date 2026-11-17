@echo off
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)
"%PYTHON%" -m replica_ledger.cli demo --workdir run\demo --report-json reports\demo_report.json --report-text reports\demo_report.txt
if errorlevel 1 exit /b 1
echo [replica-ledger] report: reports\demo_report.json
echo [replica-ledger] report: reports\demo_report.txt
endlocal
