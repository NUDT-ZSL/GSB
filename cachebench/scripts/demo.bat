@echo off
rem 云原生流水线制品缓存工作台 - 可重复演示（Windows 命令行版）
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0\.."

go build -o cachebench.exe . || exit /b 1
set BIN=cachebench.exe
set PASS=0
set FAIL=0

echo ================ 场景一：冷缓存构建 ================
call :reset
%BIN% < scripts\scenario1.txt > out\scenario1.log 2>&1
type out\scenario1.log
call :check "首次构建全部未命中" "OK build hits=0 misses=4 missed=src-config,src-schema,compiled-app,pkg-bundle" out\scenario1.log
call :check "二次构建全部命中" "OK build hits=4 misses=0 missed=" out\scenario1.log
call :check "单制品命中判定" "OK hit key=src-config result=HIT digest=sha256:f92f2765e3435bbd67f8221855fefbab578eec11918c3ee09e84d2449b963b04" out\scenario1.log

echo ================ 场景二：上游变更失效传播 ================
copy /y samples\artifacts\src-config.v2.txt samples\artifacts\src-config.txt >nul
%BIN% < scripts\scenario2.txt > out\scenario2.log 2>&1
type out\scenario2.log
call :check "失效传播集合" "OK invalidate root=src-config count=2 invalidated=compiled-app,pkg-bundle" out\scenario2.log
call :check "重建命中数" "OK build hits=1 misses=3 missed=src-config,compiled-app,pkg-bundle" out\scenario2.log
call :check "快照持久化" "OK snapshot entries=4 hits=2 traces=1" out\scenario2.log
call :check "恢复快照一致" "OK restore replaced=false diff=" out\scenario2.log
call :check "重放校验通过" "OK replay checked=3" out\scenario2.log
call :check "删除被引用制品拒绝" "ERR E_REF field=src-config" out\scenario2.log

echo ================ 场景三：缓存库降版升级 ================
copy /y samples\artifacts\src-config.v1.txt samples\artifacts\src-config.txt >nul
if exist cachestore rmdir /s /q cachestore
xcopy samples\store_v1 cachestore\ /e /i /y /q >nul
%BIN% < scripts\scenario3.txt > out\scenario3.log 2>&1
type out\scenario3.log
call :check "v1 条目自动升级" "OK open entries=4 upgraded=4 formatErrors=0" out\scenario3.log
call :check "升级键稳定序" "OK upgraded keys=compiled-app,pkg-bundle,src-config,src-schema" out\scenario3.log
if exist cachestore\checkpoint.json (echo PASS  检查点文件存在 & set /a PASS+=1) else (echo FAIL  检查点文件缺失 & set /a FAIL+=1)
call :check "升级后下游命中" "OK hit key=pkg-bundle result=HIT" out\scenario3.log

echo ================ 附加：错误码与格式互操作 ================
(
echo load samples/manifest-bad.json
echo open samples/store_bad
echo quit
) > out\extra.txt
%BIN% < out\extra.txt > out\extra.log 2>&1
type out\extra.log
call :check "清单强校验拒绝" "ERR E_MANIFEST field=stages[1].id" out\extra.log
call :check "未知版本条目拒绝" "ERR E_FORMAT field=future-art.entry.json" out\extra.log
call :check "其余条目仍可用" "OK open entries=1" out\extra.log

call :reset
echo ------------------------------------------------
echo 演示完成: PASS=%PASS% FAIL=%FAIL%
if %FAIL% neq 0 exit /b 1
exit /b 0

:check
findstr /C:"%~2" "%~3" >nul
if %errorlevel%==0 (echo PASS  %~1 & set /a PASS+=1) else (echo FAIL  %~1 ^(期望包含: %~2^) & set /a FAIL+=1)
exit /b 0

:reset
copy /y samples\artifacts\src-config.v1.txt samples\artifacts\src-config.txt >nul
if exist cachestore rmdir /s /q cachestore
if exist snapshot.json del /f /q snapshot.json
if exist out rmdir /s /q out
mkdir out
exit /b 0
