@echo off
setlocal
cd /d "%~dp0"
python -m ddp_ckpt_lab ui %*
exit /b %ERRORLEVEL%
