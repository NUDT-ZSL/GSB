@echo off
setlocal
pushd "%~dp0.."
py -3 -m mvcc_workbench --session-dir run demo --output artifacts\verification.json
set CODE=%ERRORLEVEL%
popd
exit /b %CODE%
