@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run setup_windows.ps1 first.
  exit /b 2
)

".venv\Scripts\python.exe" -m voice_repair verify ^
  --task data\task.json ^
  --work-dir state\verification ^
  --workers 3
exit /b %errorlevel%
