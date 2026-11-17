@echo off
setlocal
cd /d "%~dp0.."
if "%~1"=="" (
    echo Usage: scripts\start_node.bat SPEC_JSON NODE_ID [PORT]
    exit /b 2
)
if "%~2"=="" (
    echo Usage: scripts\start_node.bat SPEC_JSON NODE_ID [PORT]
    exit /b 2
)
set "SPEC=%~1"
set "NODE=%~2"
set "PORT=%~3"
if "%PORT%"=="" set "PORT=19101"
if exist ".venv\Scripts\python.exe" (
    set "PYTHON=%CD%\.venv\Scripts\python.exe"
) else (
    set "PYTHON=python"
)
if not exist "run" mkdir "run"
if not exist "logs" mkdir "logs"
echo [replica-ledger] starting node %NODE% on 127.0.0.1:%PORT%
start "replica-node-%NODE%" /B "%PYTHON%" -m replica_ledger.cli serve --spec "%SPEC%" --node-id "%NODE%" --host 127.0.0.1 --port %PORT% --run-dir run --log "logs\node-%NODE%.jsonl" >> "logs\node-%NODE%.stdout.log" 2>&1
endlocal
