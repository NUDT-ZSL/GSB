@echo off
rem ============================================================
rem Multi-source JSONL handover normalization - Windows entry
rem Usage:
rem   run_handover.bat ^<input-dir^> ^<mapping-config^> ^<output-dir^>
rem Example:
rem   run_handover.bat samples\input samples\config\mapping.json out
rem ============================================================
setlocal

if "%~3"=="" (
    echo Usage: run_handover.bat ^<input-dir^> ^<mapping-config^> ^<output-dir^>
    exit /b 1
)

python -I "%~dp0run_handover.py" --input-dir "%~1" --config "%~2" --output-dir "%~3"
exit /b %errorlevel%

endlocal
