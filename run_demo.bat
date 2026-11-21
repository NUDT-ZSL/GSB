@echo off
setlocal
cd /d "%~dp0"
python scripts\generate_samples.py
if errorlevel 1 exit /b 1
python -m drift_monitor --runtime runtime demo --config samples\monitor_v3.json --stream samples\data\batch_stream.json --clean
endlocal
