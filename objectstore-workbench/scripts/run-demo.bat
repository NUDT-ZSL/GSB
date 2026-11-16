@echo off
REM Deterministic end-to-end validation (in-process loopback service).
setlocal
cd /d "%~dp0\.."
python -m oswb demo --chunk-size 131072 --global-concurrency 4 --session-quota 2
endlocal
