@echo off
REM ============================================================
REM Offline dangerous-command rule auditor - Windows demo
REM No network, Python stdlib only. Usage: run_demo.cmd
REM ============================================================
setlocal
set ROOT=%~dp0
cd /d "%ROOT%"

echo ==== 0. Run 19 acceptance unit tests ====
python -I tests\test_auditor.py
if errorlevel 1 goto :fail

echo.
echo ==== 1. Audit stage: detect legacy concatenation bypasses ====
python -I auditor.py --config samples\input\rules_valid.json --commands samples\input\commands.txt --outdir samples\output\demo_audit
if errorlevel 1 goto :fail

echo.
echo ==== 2. Remediated stage: semantic per-command verdicts ====
python -I auditor.py --config samples\input\rules_valid.json --commands samples\input\commands.txt --outdir samples\output\demo_fixed --stage remediated
if errorlevel 1 goto :fail

echo.
echo ==== 3. Anomalous configs: missing fields / bad pattern / duplicate ids ====
python -I auditor.py --config samples\input\rules_invalid_fields.json --config samples\input\rules_bad_pattern.json --config samples\input\rules_duplicate.json --commands samples\input\commands.txt --outdir samples\output\demo_errors
if errorlevel 1 goto :fail

echo.
echo ==== 4. Missing input (exit code 1 expected, outputs still produced) ====
python -I auditor.py --config samples\input\not_exist.json --config samples\input\rules_valid.json --commands samples\input\commands.txt --outdir samples\output\demo_missing --retry 0
echo (exit code 1 above is expected)

echo.
echo ==== 5. No arguments (usage printed, exit code 2 expected) ====
python -I auditor.py
echo (exit code 2 above is expected)

echo.
echo ==== 6. Transient unreadable file retry demo ====
python -I examples\demo_retry.py
if errorlevel 1 goto :fail

echo.
echo All demos finished. Result files: samples\output\
exit /b 0

:fail
echo Demo failed, inspect output above.
exit /b 1
