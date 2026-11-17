@echo off
setlocal
cd /d "%~dp0\.."
REM Usage: run_server.cmd <port> <workdir>
python -m gateway serve --port %1 --routes samples\basic\routes.json --tokens samples\basic\tokens.json --workdir %2
endlocal
