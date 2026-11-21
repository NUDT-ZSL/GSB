@echo off
setlocal
cd /d "%~dp0\.."
python -m bmm_pipeline.cli demo --work-dir .demo --fault-after-blocks 20
endlocal
