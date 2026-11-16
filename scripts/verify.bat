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
if not exist build mkdir build

echo [1/9] go test
go test ./... -count=1
if errorlevel 1 goto :fail

echo [2/9] go vet
go vet ./...
if errorlevel 1 goto :fail

echo [3/9] build Windows binary
go build -o build\docquery.exe .\cmd\docquery
if errorlevel 1 goto :fail

echo [4/9] demo byte-stability check
build\docquery.exe demo -root . -snapshot artifacts\demo-session.json -log artifacts\demo-a.log > artifacts\demo-a.json
if errorlevel 1 goto :fail
build\docquery.exe demo -root . -snapshot artifacts\demo-session.json -log artifacts\demo-b.log > artifacts\demo-b.json
if errorlevel 1 goto :fail
fc /b artifacts\demo-a.json artifacts\demo-b.json >nul
if errorlevel 1 (
  echo Demo output is not byte-identical.
  goto :fail
)

echo [5/9] scan/index validation
build\docquery.exe validate -root . -log artifacts\validate.log > artifacts\validate-output.json
if errorlevel 1 goto :fail

echo [6/9] empty-index validation degradation
build\docquery.exe validate -root . -indexes= -log artifacts\validate-empty.log > artifacts\validate-empty-output.json
if errorlevel 1 goto :fail

echo [7/9] inject E_DOC
build\docquery.exe inject -kind doc-corruption -root . -log artifacts\inject-doc.log > artifacts\inject-doc.json
if errorlevel 1 goto :fail

echo [8/9] inject E_CACHE
build\docquery.exe inject -kind cache-stale -root . -log artifacts\inject-cache.log > artifacts\inject-cache.json
if errorlevel 1 goto :fail

echo [9/9] inject E_REPLAY
build\docquery.exe inject -kind replay-mismatch -root . -log artifacts\inject-replay.log > artifacts\inject-replay.json
if errorlevel 1 goto :fail

echo Validation passed. Outputs are under artifacts\.
exit /b 0

:fail
echo Validation failed.
exit /b 1
