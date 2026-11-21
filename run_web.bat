@echo off
setlocal
cd /d "%~dp0"
python -m slotline serve --host 127.0.0.1 --port 8765
if errorlevel 1 pause
endlocal
