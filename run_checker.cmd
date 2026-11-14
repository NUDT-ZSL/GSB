@echo off
setlocal EnableExtensions
REM Windows entry point for the subtitle checker.
REM
REM Usage:
REM   run_checker.cmd <manifest.json> <frames_dir> <output_dir>
REM
REM Uses the .venv created by setup_windows.cmd when present, otherwise the
REM python interpreter on PATH (3.11+ with Pillow installed).

cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m subtitle_checker %*
) else (
    python -m subtitle_checker %*
)

set "CODE=%ERRORLEVEL%"
endlocal & exit /b %CODE%
