@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

%PYTHON% main.py demo
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if not "%EXIT_CODE%"=="0" echo Demo failed with exit code %EXIT_CODE%.
pause
exit /b %EXIT_CODE%

