@echo off
setlocal
python "%~dp0run_workbench.py" %*
exit /b %ERRORLEVEL%

