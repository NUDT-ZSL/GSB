@echo off
setlocal
cd /d "%~dp0"
python -m alarm_domain ui %*
exit /b %errorlevel%
