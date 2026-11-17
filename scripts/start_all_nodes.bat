@echo off
setlocal
cd /d "%~dp0.."
call scripts\start_node.bat samples\scenarios\multi\spec.json n1 19101
call scripts\start_node.bat samples\scenarios\multi\spec.json n2 19102
call scripts\start_node.bat samples\scenarios\multi\spec.json n3 19103
call scripts\start_node.bat samples\scenarios\multi\spec.json n4 19104
call scripts\start_node.bat samples\scenarios\multi\spec.json n5 19105
call scripts\start_node.bat samples\scenarios\multi\spec.json n6 19106
echo [replica-ledger] all sample nodes started; PID records are in run\node-*.json
endlocal
