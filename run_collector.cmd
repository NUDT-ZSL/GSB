@echo off
setlocal
rem Local authorized mirror collector (Windows command line).
rem Usage: run_collector.cmd --config config\sample.json --out output
python -m collector %*
exit /b %ERRORLEVEL%
