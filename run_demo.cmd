@echo off
setlocal
cd /d %~dp0
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 -m pip install -r requirements.txt
    py -3 demo.py
) else (
    python -m pip install -r requirements.txt
    python demo.py
)
endlocal
