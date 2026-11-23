@echo off
setlocal

python -m audit_ledger load ^
  --db audit\audit.db ^
  --events samples\events.json ^
  --quotas samples\quotas.json ^
  --policy samples\policy.json
if errorlevel 1 goto :error

python -m audit_ledger write --db audit\audit.db
if errorlevel 1 goto :error

python -m audit_ledger status --db audit\audit.db
if errorlevel 1 goto :error

python -m audit_ledger verify ^
  --db audit\audit.db ^
  --expected samples\expected_summary.json
if errorlevel 1 goto :error

echo Audit ledger example completed successfully.
exit /b 0

:error
echo Audit ledger example failed.
exit /b 1
