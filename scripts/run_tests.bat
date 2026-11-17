@echo off
setlocal
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python -m unittest discover -s tests -v
if errorlevel 1 exit /b 1
python scripts\verify_recovery.py
exit /b %errorlevel%
