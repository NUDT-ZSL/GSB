@echo off
setlocal
cd /d "%~dp0"

python -m pip install -r requirements.txt
if errorlevel 1 exit /b 1

python generate_samples.py --workspace . --seed 20260214 --force
if errorlevel 1 exit /b 1

python run_pipeline.py --workspace .
if errorlevel 1 exit /b 1

python verify_results.py --workspace .
if errorlevel 1 exit /b 1

endlocal
