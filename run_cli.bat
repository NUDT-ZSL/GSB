@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%run_cli.py" %*
) else (
  python "%ROOT%run_cli.py" %*
)
exit /b %ERRORLEVEL%
