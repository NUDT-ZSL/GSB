@echo off
REM Start the loopback TCP storage service simulation on Windows.
setlocal
cd /d "%~dp0\.."
if "%OSWB_PORT%"=="" set OSWB_PORT=9101
python -m oswb --host 127.0.0.1 --port %OSWB_PORT% --base-dir runtime\workbench-data --log-dir runtime\logs serve --echo
endlocal
