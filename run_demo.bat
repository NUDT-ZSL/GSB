@echo off
setlocal
set "PYTHONPATH=%~dp0src;%PYTHONPATH%"
py -3 -m panoweave demo --samples samples --out artifacts --clean %*
endlocal

