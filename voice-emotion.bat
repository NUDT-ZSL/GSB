@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
  "%~dp0.venv\Scripts\python.exe" -m voice_state.cli %*
) else (
  py -3.11 -m voice_state.cli %*
)
exit /b %errorlevel%

