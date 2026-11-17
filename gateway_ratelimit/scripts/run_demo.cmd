@echo off
REM Deterministic end-to-end demo on Windows (no containers, no external services).
setlocal
cd /d "%~dp0\.."
if exist workdir rmdir /s /q workdir
if exist reports rmdir /s /q reports
python tools\demo.py
endlocal
