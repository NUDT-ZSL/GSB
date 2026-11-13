@echo off
REM 启动“已修复形态”靶场服务（fixed：增加资源归属校验，非所有者返回 403）
REM 用法: run_fixed.bat [端口]  默认端口 8082
setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8082
python -I range\server.py --host 127.0.0.1 --port %PORT% --mode fixed
endlocal
