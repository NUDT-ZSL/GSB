@echo off
REM Windows 运行入口：生成样本并执行完整管线
setlocal
cd /d "%~dp0.."
set "PYTHONPATH=%CD%\src;%PYTHONPATH%"
python -m xling_pipeline.generate_data data\sample
if errorlevel 1 exit /b 1
python -m xling_pipeline.cli configs\pipeline_config.json
exit /b %errorlevel%
