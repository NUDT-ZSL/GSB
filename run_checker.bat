@echo off
REM 运行越权修复对比判定工具
REM 用法:
REM   run_checker.bat                      使用默认 scenarios.json / report.json
REM   run_checker.bat 清单路径 报告路径    自定义路径
setlocal
set MANIFEST=%1
if "%MANIFEST%"=="" set MANIFEST=scenarios.json
set REPORT=%2
if "%REPORT%"=="" set REPORT=report.json
python -I checker.py "%MANIFEST%" --output "%REPORT%" --vuln-url http://127.0.0.1:8081 --fixed-url http://127.0.0.1:8082
echo.
echo 判定工具退出码: %ERRORLEVEL%
endlocal
