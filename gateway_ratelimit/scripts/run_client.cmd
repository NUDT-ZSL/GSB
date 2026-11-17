@echo off
setlocal
cd /d "%~dp0\.."
REM Usage: run_client.cmd <port> <out.json>
python tools\mock_client.py --host 127.0.0.1 --port %1 --requests samples\basic\requests.ndjson --out %2
endlocal
