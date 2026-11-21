@echo off
setlocal
cd /d "%~dp0"
python -m slotline demo
if errorlevel 1 pause
endlocal
