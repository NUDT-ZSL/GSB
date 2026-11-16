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

%PYTHON% main.py gui
if errorlevel 1 pause
exit /b %ERRORLEVEL%

