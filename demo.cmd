@echo off
rem Reproducible acceptance demo for secscan. Run from the project root:
rem     demo.cmd
rem Requires only Python 3.10+ (no third-party packages, no network access).
setlocal
set PY=python
if not "%~1"=="" set PY=%~1

set SAMPLES=samples
set DEMO=reports\demo
if not exist "%DEMO%" mkdir "%DEMO%"

echo === [0] Python version ===
%PY% --version || goto :fail

echo.
echo === [1] Missing config: expect non-zero exit, stderr reason, no report ===
%PY% secscan.py --config "%SAMPLES%\missing.json" --output "%DEMO%\should-not-exist.json"
echo EXIT=%ERRORLEVEL%
if not "%ERRORLEVEL%"=="3" (echo UNEXPECTED exit code for missing config & goto :fail)
if exist "%DEMO%\should-not-exist.json" (echo UNEXPECTED report file & goto :fail)

echo.
echo === [2] Baseline scan: weak password, plaintext secret, dangerous loader ===
%PY% secscan.py --config "%SAMPLES%\vulnerable.json" --output "%DEMO%\report.json" || goto :fail
%PY% -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); assert d['total']==len(d['vulnerabilities'])==4, d['total']; print('CHECKS: total=4 and total==len(vulnerabilities)')" "%DEMO%\report.json" || goto :fail

echo.
echo === [3] Second scan: determinism check (same ordering) ===
%PY% secscan.py --config "%SAMPLES%\vulnerable.json" --output "%DEMO%\report2.json" || goto :fail
fc /b "%DEMO%\report.json" "%DEMO%\report2.json" >nul && echo REPORTS IDENTICAL || (echo REPORTS DIFFER & goto :fail)

echo.
echo === [4] Missing rules file: expect warning + default rules, still exit 0 ===
%PY% secscan.py --config "%SAMPLES%\vulnerable.json" --output "%DEMO%\report-default-rules.json" --rules "%SAMPLES%\no-such-rules.json" || goto :fail

echo.
echo === [5] Custom rules file ===
%PY% secscan.py --config "%SAMPLES%\vulnerable.json" --output "%DEMO%\report-custom-rules.json" --rules "%SAMPLES%\rules.json" || goto :fail

echo.
echo === [6] Fix mode: replace weak password + plaintext secrets, keep loader ===
%PY% secscan.py --config "%SAMPLES%\vulnerable.json" --output "%DEMO%\report-fixed.json" --fix --fixed-config "%DEMO%\vulnerable.fixed.json" || goto :fail

echo.
echo === [7] Re-scan the fixed file: expect only the loader finding ===
%PY% secscan.py --config "%DEMO%\vulnerable.fixed.json" --output "%DEMO%\report-rescan.json" || goto :fail
%PY% -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); assert d['total']==1 and d['vulnerabilities'][0]['rule_type']=='dangerous_deserialization', d['vulnerabilities']; print('CHECKS: only dangerous_deserialization remains')" "%DEMO%\report-rescan.json" || goto :fail

echo.
echo === [8] No arguments: expect non-zero exit and usage text ===
%PY% secscan.py
if not "%ERRORLEVEL%"=="2" (echo UNEXPECTED exit code for missing arguments & goto :fail)

echo.
echo === [9] Clean sample: expect zero findings ===
%PY% secscan.py --config "%SAMPLES%\clean.json" --output "%DEMO%\report-clean.json" || goto :fail

echo.
echo ALL DEMO STEPS COMPLETED. Reports are under %DEMO%.
exit /b 0

:fail
echo.
echo DEMO FAILED with exit code %ERRORLEVEL%.
exit /b 1
