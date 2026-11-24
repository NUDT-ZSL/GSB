@echo off
setlocal
python "%~dp0apply_local_edit.py" %*
exit /b %ERRORLEVEL%
