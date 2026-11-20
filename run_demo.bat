@echo off
setlocal
cd /d "%~dp0"
python -m blockops demo --data-dir samples --output-dir build\demo
exit /b %errorlevel%
