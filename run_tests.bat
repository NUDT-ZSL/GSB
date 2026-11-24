@echo off
setlocal
cd /d "%~dp0"
python -m pip install numpy pytest
python -m pytest
endlocal

