@echo off
setlocal
cd /d "%~dp0"
python demo.py
set EXIT_CODE=%ERRORLEVEL%
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Demo failed with exit code %EXIT_CODE%. See artifacts\demo\demo_error.json
)
exit /b %EXIT_CODE%

