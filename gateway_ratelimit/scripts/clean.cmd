@echo off
setlocal
cd /d "%~dp0\.."
if exist workdir rmdir /s /q workdir
if exist reports rmdir /s /q reports
echo cleaned workdir and reports (cold start is reproducible)
endlocal
