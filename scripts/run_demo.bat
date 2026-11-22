@echo off
setlocal
cd /d "%~dp0\.."
py -3.11 -m quantlab.cli demo --crash-after-layer 1
if errorlevel 1 exit /b %errorlevel%
py -3.11 -m quantlab.cli serve --port 8765
