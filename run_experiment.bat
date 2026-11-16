@echo off
setlocal
cd /d "%~dp0"
python experiment.py %*
exit /b %errorlevel%
