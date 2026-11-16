@echo off
setlocal
cd /d "%~dp0"
python -m ddp_ckpt_lab.demo %*
exit /b %ERRORLEVEL%
