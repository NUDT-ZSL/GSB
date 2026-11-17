@echo off
REM Windows 一键演示：生成确定性样本并运行处理引擎
setlocal
cd /d %~dp0
if not exist demo_workspace mkdir demo_workspace
python scripts\generate_samples.py --spec scripts\demo_spec.json --out demo_workspace
if errorlevel 1 exit /b 1
python -m mocap_audio.cli --session demo_workspace\session.json
exit /b %errorlevel%
