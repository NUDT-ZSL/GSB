@echo off
setlocal
cd /d "%~dp0"
python -m sqlplan_lab gui --db lab.sqlite3
exit /b %errorlevel%
