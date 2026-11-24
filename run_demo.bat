@echo off
setlocal
cd /d "%~dp0"
python -m audio_pipeline demo --workspace "%~dp0demo" --force
echo.
echo Demo exit code %ERRORLEVEL% (3 is expected because of injected anomalies).
exit /b %ERRORLEVEL%
