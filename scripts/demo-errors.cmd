@echo off
setlocal
cd /d "%~dp0.."

if not exist bin mkdir bin
go build -o bin\ledger.exe .\cmd\ledger
if errorlevel 1 exit /b 1
if not exist data\errors mkdir data\errors

echo [E_DEPEND] Startup injection: dependency cycle
bin\ledger.exe apply --spec samples\manifest-v1.json --db data\errors\cycle.db --session errors --inject dependency-cycle --log data\errors\cycle.jsonl
echo exit_code=%ERRORLEVEL%

echo [E_SPEC] Startup injection: corrupt manifest
bin\ledger.exe apply --spec samples\manifest-v1.json --db data\errors\corrupt.db --session errors --inject corrupt-spec --log data\errors\corrupt.jsonl
echo exit_code=%ERRORLEVEL%

echo [E_CAPACITY] Startup injection: zero cache capacity
bin\ledger.exe apply --spec samples\manifest-v1.json --db data\errors\zero.db --session errors --inject zero-capacity --log data\errors\zero.jsonl
echo exit_code=%ERRORLEVEL%

echo [E_DEPEND] Natural cyclic manifest sample
bin\ledger.exe apply --spec samples\manifest-cycle.json --db data\errors\natural-cycle.db --session errors --log data\errors\natural-cycle.jsonl
echo exit_code=%ERRORLEVEL%

echo [E_SPEC] Natural broken manifest sample
bin\ledger.exe apply --spec samples\manifest-broken.json --db data\errors\broken.db --session errors --log data\errors\broken.jsonl
echo exit_code=%ERRORLEVEL%

echo [E_REF] Deleting an entry that is still referenced
if exist data\errors\ref.db del /q data\errors\ref.db
if exist data\errors\ref.db-wal del /q data\errors\ref.db-wal
if exist data\errors\ref.db-shm del /q data\errors\ref.db-shm
bin\ledger.exe apply --spec samples\manifest-v1.json --db data\errors\ref.db --session errors --log data\errors\ref.jsonl
if errorlevel 1 exit /b 1
bin\ledger.exe apply --spec samples\manifest-delete-referenced.json --db data\errors\ref.db --session errors --log data\errors\ref.jsonl
echo exit_code=%ERRORLEVEL%

echo Error-code demo completed. Logs are under data\errors.
endlocal
