@echo off
setlocal
.venv\Scripts\python -m recon_workbench generate --out sample_data --views 8 --seed 20261117 --width 640 --height 480 --markers 72
exit /b %ERRORLEVEL%
