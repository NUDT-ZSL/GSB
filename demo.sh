#!/usr/bin/env bash
# Reproducible acceptance demo for secscan (Git Bash / WSL / Linux).
# Run from the project root:  bash demo.sh
set -u

PY=${PY:-python3}
command -v "$PY" >/dev/null 2>&1 || PY=python
DEMO=reports/demo
mkdir -p "$DEMO"
status=0

step() { printf '\n=== %s ===\n' "$1"; }
check() { if [ "$1" -ne "$2" ]; then echo "UNEXPECTED exit code $1 (wanted $2)"; status=1; fi; }

step "[0] Python version"
"$PY" --version || exit 1

step "[1] Missing config: expect non-zero exit, stderr reason, no report"
"$PY" -I secscan.py --config samples/missing.json --output "$DEMO/should-not-exist.json"
check $? 3
[ -e "$DEMO/should-not-exist.json" ] && { echo "UNEXPECTED report file"; status=1; }

step "[2] Baseline scan"
"$PY" -I secscan.py --config samples/vulnerable.json --output "$DEMO/report.json"
check $? 0
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); assert d['total']==len(d['vulnerabilities'])==4; print('CHECKS: total=4 and total==len(vulnerabilities)')" "$DEMO/report.json" || status=1

step "[3] Second scan: determinism check"
"$PY" -I secscan.py --config samples/vulnerable.json --output "$DEMO/report2.json"
cmp -s "$DEMO/report.json" "$DEMO/report2.json" && echo "REPORTS IDENTICAL" || { echo "REPORTS DIFFER"; status=1; }

step "[4] Missing rules file: warning + defaults, still exit 0"
"$PY" -I secscan.py --config samples/vulnerable.json --output "$DEMO/report-default-rules.json" --rules samples/no-such-rules.json
check $? 0

step "[5] Custom rules file"
"$PY" -I secscan.py --config samples/vulnerable.json --output "$DEMO/report-custom-rules.json" --rules samples/rules.json
check $? 0

step "[6] Fix mode"
"$PY" -I secscan.py --config samples/vulnerable.json --output "$DEMO/report-fixed.json" --fix --fixed-config "$DEMO/vulnerable.fixed.json"
check $? 0

step "[7] Re-scan fixed file: only the loader finding remains"
"$PY" -I secscan.py --config "$DEMO/vulnerable.fixed.json" --output "$DEMO/report-rescan.json"
check $? 0
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1],encoding='utf-8')); assert d['total']==1 and d['vulnerabilities'][0]['rule_type']=='dangerous_deserialization'; print('CHECKS: only dangerous_deserialization remains')" "$DEMO/report-rescan.json" || status=1

step "[8] No arguments: usage + non-zero exit"
"$PY" -I secscan.py
check $? 2

step "[9] Clean sample: zero findings"
"$PY" -I secscan.py --config samples/clean.json --output "$DEMO/report-clean.json"
check $? 0

echo
if [ "$status" -eq 0 ]; then
  echo "ALL DEMO STEPS COMPLETED. Reports are under $DEMO."
else
  echo "DEMO FAILED."
fi
exit "$status"
