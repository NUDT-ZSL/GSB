@echo off
REM Local deterministic end-to-end acceptance demo (Windows, no Docker/GPU).
setlocal
cd /d "%~dp0\.."
python scripts\run_demo.py
endlocal
