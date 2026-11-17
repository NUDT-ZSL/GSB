@echo off
REM 确定性三场景演示（Windows，无需任何第三方依赖）
REM 在项目根目录双击或执行：scripts\run_demo.bat
setlocal
cd /d "%~dp0.."
python --version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] python not found on PATH. Install Python 3.10+ with tcl/tk enabled.
  exit /b 1
)
python scripts\demo.py
endlocal
