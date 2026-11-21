@echo off
setlocal
cd /d "%~dp0"
py -3.11 demo.py %*
exit /b %errorlevel%
