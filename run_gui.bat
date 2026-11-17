@echo off
setlocal
cd /d "%~dp0"
python -m colpipe gui --workspace .
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
  echo.
  echo GUI exited with code %EXIT_CODE%.
)
exit /b %EXIT_CODE%

