@echo off
setlocal
if not exist .venv\Scripts\python.exe (
  echo Create .venv and install requirements first:
  echo   python -m venv .venv
  echo   .venv\Scripts\python -m pip install -r requirements.txt
  exit /b 2
)
.venv\Scripts\python -m recon_workbench %*
exit /b %ERRORLEVEL%
