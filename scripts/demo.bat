@echo off
setlocal EnableExtensions
cd /d "%~dp0\.."

set "GOTOOLCHAIN=local"
set "GOPROXY=off"
set "GOFLAGS=-mod=mod"

where go >nul 2>nul
if errorlevel 1 (
  echo Go 1.21 or newer is required and must be in PATH.
  exit /b 1
)

if not exist artifacts mkdir artifacts

go run ./cmd/docquery demo -root . -log artifacts\demo.log
exit /b %errorlevel%
