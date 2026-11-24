@echo off
REM matgen Windows 命令提示符入口，转调 PowerShell 演示脚本
powershell -ExecutionPolicy Bypass -File "%~dp0demo.ps1"
