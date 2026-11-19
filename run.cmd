@echo off
setlocal
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found on PATH.
  exit /b 1
)
python -m annotation_app %*
exit /b %ERRORLEVEL%

