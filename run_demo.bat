@echo off
REM ============================================================
REM  LFI authorized batch health-check - one-click acceptance demo
REM  Standard library only, no network access required.
REM  Usage: run this file from a Windows command prompt (cmd.exe)
REM ============================================================
setlocal
chcp 65001 >nul
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -I run_demo.py
) else (
    python -I run_demo.py
)
set RC=%errorlevel%
echo.
echo demo exit code: %RC%
endlocal & exit /b %RC%
