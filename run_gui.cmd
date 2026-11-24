@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if %ERRORLEVEL%==0 (
  py -3.11 -m screen_diag gui --samples samples
) else (
  python -m screen_diag gui --samples samples
)
pause
