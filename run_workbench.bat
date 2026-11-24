@echo off
setlocal
python "%~dp0tools\run_workbench.py"
exit /b %ERRORLEVEL%
