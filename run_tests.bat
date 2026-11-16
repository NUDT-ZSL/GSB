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

%PYTHON% -m unittest discover -s tests -v
set "EXIT_CODE=%ERRORLEVEL%"
echo.
if "%EXIT_CODE%"=="0" echo All tests passed.
pause
exit /b %EXIT_CODE%

