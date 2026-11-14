@echo off
setlocal EnableExtensions
REM One-time Windows setup: create .venv with Python 3.11 and install Pillow.
REM Candidate interpreters: py -3.11, python3.11, uv, then python (3.11+).

cd /d "%~dp0"

set "PYCMD="

where py >nul 2>nul
if not defined PYCMD (
    py -3.11 -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        py -3.11 -m venv .venv
        if not errorlevel 1 set "PYCMD=py -3.11"
    )
)

if not defined PYCMD (
    where python3.11 >nul 2>nul
    if not errorlevel 1 (
        python3.11 -m venv .venv
        if not errorlevel 1 set "PYCMD=python3.11"
    )
)

if not defined PYCMD (
    where uv >nul 2>nul
    if not errorlevel 1 (
        uv venv --python 3.11 .venv >nul
        if not errorlevel 1 set "PYCMD=uv"
    )
)

if not defined PYCMD (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>nul
        if not errorlevel 1 (
            python -m venv .venv
            if not errorlevel 1 set "PYCMD=python"
        )
    )
)

if not defined PYCMD (
    echo [ERROR] Python 3.11 or newer was not found on PATH.
    echo         Install Python 3.11 and retry, or run: uv venv --python 3.11 .venv
    exit /b 2
)

".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --quiet "Pillow>=10.0"
if errorlevel 1 (
    echo [ERROR] Installing Pillow failed.
    exit /b 4
)

echo Setup complete: .venv is ready ^(created via %PYCMD%^). Run run_checker.cmd next.

endlocal & exit /b 0
