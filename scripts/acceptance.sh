#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo '[1/4] 生成确定性样本'
python3 scripts/generate_sample.py --out samples

echo '[2/4] 运行单元/集成测试'
python3 -m unittest -v tests.test_workbench

echo '[3/4] 生成全排列/恢复/并发验收证明'
python3 scripts/verify_acceptance.py > artifacts/acceptance-report.stdout.json

echo '[4/4] CLI 基线与 verify 比对'
DB="artifacts/cli-acceptance-$$.db"
python3 -m workbench load --db "$DB" --registry samples/registry.json --clock-ms 1760000000000 >/dev/null
python3 -m workbench merge --db "$DB" \
  --v1 samples/rules_v1.json --v2-manifest samples/rules_v2_manifest.json \
  --clock-ms 1760000000000 \
  --registry-for-diff samples/registry.json \
  --observations-for-diff samples/observations.jsonl \
  --out artifacts/merge-decision.json >/dev/null
python3 -m workbench load --db "$DB" --observations samples/observations.jsonl >/dev/null
python3 -m workbench reconcile --db "$DB" --clock-ms 1760000000000 >/dev/null
python3 -m workbench status --db "$DB" --out artifacts/cli-baseline-ledger.json >/dev/null
python3 -m workbench verify --expected artifacts/cli-baseline-ledger.json --actual-db "$DB"

echo 'ACCEPTANCE_OK'
