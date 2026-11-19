@echo off
setlocal
cd /d "%~dp0\.."
py -3.11 scripts\run_demo.py
exit /b %ERRORLEVEL%
