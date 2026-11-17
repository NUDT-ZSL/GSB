@echo off
REM 启动 Tkinter 可视化工作台（Windows 桌面会话）
setlocal
cd /d "%~dp0.."
python -m obsdiag gui --workdir output\gui
endlocal
