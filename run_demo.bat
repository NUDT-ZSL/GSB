@echo off
setlocal
cd /d "%~dp0"
python -m pip install numpy pytest
python -m pytest
python -m mfa_guard --root . demo
python -m mfa_guard --root . verify
endlocal

