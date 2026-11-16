@echo off
setlocal
set "ROOT=%~dp0"
if exist "%ROOT%.venv\Scripts\python.exe" (
  "%ROOT%.venv\Scripts\python.exe" "%ROOT%run_gui.py" %*
) else (
  python "%ROOT%run_gui.py" %*
)
exit /b %ERRORLEVEL%
