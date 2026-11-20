@echo off
setlocal
cd /d "%~dp0"
python scripts\generate_samples.py --output-dir samples
exit /b %errorlevel%
