@echo off
setlocal
set PYTHONUTF8=1
cd /d "%~dp0.."
python -m infer_workbench demo --out-dir demo_out
endlocal
