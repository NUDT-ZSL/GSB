@echo off
setlocal
cd /d "%~dp0.."

echo [replica-ledger] project root: %CD%
where py >nul 2>nul
if errorlevel 1 (
    set "PYTHON_BOOT=python"
) else (
    set "PYTHON_BOOT=py -3.11"
)

if exist ".venv\Scripts\python.exe" goto :have_venv
if exist ".venv" rmdir /s /q ".venv"
%PYTHON_BOOT% -m venv .venv
if errorlevel 1 exit /b 1

:have_venv
call ".venv\Scripts\activate.bat"
python -m pip install --upgrade pip
if errorlevel 1 exit /b 1
python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1
python -m pip install -e .
if errorlevel 1 exit /b 1

echo [replica-ledger] Python and numpy versions:
python -c "import sys, numpy; print(sys.version); print('numpy', numpy.__version__)"
python -c "import tkinter; print('Tkinter', tkinter.TkVersion)" 2>nul
if errorlevel 1 echo [replica-ledger] WARNING: Tkinter is missing; reinstall Windows Python with Tcl/Tk.

python -m replica_ledger.cli generate --output samples --force
if errorlevel 1 exit /b 1
echo [replica-ledger] setup complete. Next: scripts\run_demo.bat
endlocal
