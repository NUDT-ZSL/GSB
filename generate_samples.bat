@echo off
setlocal
python "%~dp0tools\generate_samples.py" %*
exit /b %ERRORLEVEL%
