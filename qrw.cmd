@echo off
setlocal
set "QRW_ROOT=%~dp0"
set "PYTHONPATH=%QRW_ROOT%src;%PYTHONPATH%"
if exist "%QRW_ROOT%.venv\Scripts\python.exe" (
  "%QRW_ROOT%.venv\Scripts\python.exe" -m qrw %*
) else (
  python -m qrw %*
)
exit /b %ERRORLEVEL%

