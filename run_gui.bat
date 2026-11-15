@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Missing .venv. Run: powershell -ExecutionPolicy Bypass -File setup_windows.ps1
  exit /b 1
)
if "%~1"=="" (
  ".venv\Scripts\python.exe" -m tactical_engine gui
) else (
  ".venv\Scripts\python.exe" -m tactical_engine gui %*
)
