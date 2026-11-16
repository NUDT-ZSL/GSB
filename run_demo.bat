@echo off
REM Local deterministic inpainting/style-transfer editor demo (Windows)
REM Requires Python 3.10+ with NumPy and Pillow installed; no network access.
setlocal
cd /d %~dp0
python scripts\generate_samples.py
if errorlevel 1 exit /b 1
python scripts\run_demo.py
exit /b %errorlevel%
