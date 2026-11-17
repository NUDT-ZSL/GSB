@echo off
setlocal
cd /d %~dp0\..
py -3.11 --version || exit /b %ERRORLEVEL%
py -3.11 -m cluster_ledger validate --config data\clusters.json --sample-data data\sample_data.json || exit /b %ERRORLEVEL%
py -3.11 -m cluster_ledger demo --workdir var\demo || exit /b %ERRORLEVEL%
echo Report: var\demo\report.json
echo Structured log: var\demo\events.jsonl
endlocal

