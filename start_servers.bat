@echo off
REM 内部工单系统 - 附件预览服务启动脚本（Windows）
REM 用法：start_servers.bat
REM 启动后：漏洞版 http://127.0.0.1:5000 ，修复版 http://127.0.0.1:5001
setlocal
cd /d "%~dp0"

start "p66-vulnerable-5000" cmd /k python preview_server\app.py --variant vulnerable --port 5000
start "p66-fixed-5001"      cmd /k python preview_server\app.py --variant fixed      --port 5001

echo 已在新窗口启动两个本地实例：
echo   漏洞版  http://127.0.0.1:5000
echo   修复版  http://127.0.0.1:5001
endlocal
