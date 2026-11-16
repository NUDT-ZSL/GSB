@echo off
setlocal
cd /d %~dp0
if "%~1"=="" (
    echo Usage: run_ui.cmd path\to\snapshot.json
    exit /b 1
)
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m orchestrator.cli ui --snapshot %1
) else (
    python -m orchestrator.cli ui --snapshot %1
)
endlocal
