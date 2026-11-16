@echo off
REM Windows 本地演示入口
setlocal
cd /d %~dp0
set PYTHONPATH=src;%PYTHONPATH%
python samples\generate_samples.py --out data\events.ndjson
python -m sre_guard.cli --base-dir . %*
endlocal
