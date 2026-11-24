@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup_windows.ps1 first.
  exit /b 2
)

if not exist "data\task.json" (
  ".venv\Scripts\python.exe" -m voice_repair generate --data-dir data --seed 20261125
  if errorlevel 1 exit /b 1
)

".venv\Scripts\python.exe" -m voice_repair run ^
  --task data\task.json ^
  --ledger state\ledger.sqlite ^
  --out-dir state\output ^
  --report state\report.json ^
  --state-dir state
exit /b %errorlevel%
