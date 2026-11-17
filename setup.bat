@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher "py" was not found. Install Python 3.11 for Windows first. 1>&2
  exit /b 1
)
py -3.11 -c "import tkinter, sqlite3; print('tkinter/sqlite3 OK')"
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
  if exist ".venv" rmdir /s /q ".venv"
  py -3.11 -m venv .venv
)
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e ".[test]"
".venv\Scripts\python.exe" -c "import numpy; print('numpy', numpy.__version__)"
exit /b %ERRORLEVEL%
