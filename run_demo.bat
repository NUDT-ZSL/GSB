@echo off
setlocal
python "%~dp0run_demo.py" %*
exit /b %ERRORLEVEL%

