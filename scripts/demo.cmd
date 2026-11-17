@echo off
setlocal
cd /d "%~dp0.."

echo [1/7] Downloading Go modules...
go mod download
if errorlevel 1 exit /b 1

echo [2/7] Building Windows executable...
if not exist bin mkdir bin
go build -o bin\ledger.exe .\cmd\ledger
if errorlevel 1 exit /b 1

if not exist data\demo mkdir data\demo
set DB=data\demo\ledger.db
set LOG=data\demo\events.jsonl
if exist "%DB%" del /q "%DB%"
if exist "%DB%-wal" del /q "%DB%-wal"
if exist "%DB%-shm" del /q "%DB%-shm"
if exist "%LOG%" del /q "%LOG%"

echo [3/7] Applying manifest v1...
bin\ledger.exe apply --spec samples\manifest-v1.json --db "%DB%" --options samples\options-demo.json --log "%LOG%"
if errorlevel 1 exit /b 1

echo [4/7] Applying manifest v2 (incremental invalidation)...
bin\ledger.exe apply --spec samples\manifest-v2.json --db "%DB%" --options samples\options-demo.json --log "%LOG%"
if errorlevel 1 exit /b 1

echo [5/7] Applying manifest v3 (modify and delete)...
bin\ledger.exe apply --spec samples\manifest-v3.json --db "%DB%" --options samples\options-demo.json --log "%LOG%"
if errorlevel 1 exit /b 1

echo [6/7] Verifying ledger and replaying snapshot...
bin\ledger.exe verify --db "%DB%" --session demo --log "%LOG%"
if errorlevel 1 exit /b 1
bin\ledger.exe replay --db "%DB%" --session demo --log "%LOG%"
if errorlevel 1 exit /b 1

echo [7/7] Restoring v1 as a whole snapshot replacement...
bin\ledger.exe restore --db "%DB%" --session demo --version v1 --log "%LOG%"
if errorlevel 1 exit /b 1
bin\ledger.exe versions --db "%DB%" --session demo --log "%LOG%"
if errorlevel 1 exit /b 1

echo Demo completed. SQLite: %DB%
echo Structured log: %LOG%
endlocal
