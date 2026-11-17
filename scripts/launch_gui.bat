@echo off
setlocal
cd /d "%~dp0.."
if exist ".venv\Scripts\pythonw.exe" (
    start "replica-ledger-gui" ".venv\Scripts\pythonw.exe" -m replica_ledger.cli gui --spec samples\scenarios\grid\spec.json --scale 8 --log logs\gui.jsonl
) else (
    start "replica-ledger-gui" pythonw -m replica_ledger.cli gui --spec samples\scenarios\grid\spec.json --scale 8 --log logs\gui.jsonl
)
endlocal
