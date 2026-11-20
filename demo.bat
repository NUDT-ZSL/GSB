@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" scripts\demo.py
) else (
  python scripts\demo.py
)
exit /b %ERRORLEVEL%

