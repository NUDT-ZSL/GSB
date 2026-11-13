@echo off
REM 启动“未修复形态”靶场服务（vulnerable：无归属校验，存在水平越权）
REM 用法: run_vulnerable.bat [端口]  默认端口 8081
setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8081
python -I range\server.py --host 127.0.0.1 --port %PORT% --mode vulnerable
endlocal
