#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

NOW_MS=1764000000000
OUT=out/acceptance

rm -rf "$OUT"
mkdir -p "$OUT"

echo '[1/7] generate deterministic samples'
python3 generate_sample.py --out-dir data/generated

echo '[2/7] unit and fault-injection tests'
python3 -m unittest discover -s tests

echo '[3/7] targeted recomputation from baseline to effective config'
python3 -m risk_governance.cli run \
  --workdir "$OUT/targeted" \
  --config data/generated/calibration_config.baseline.json \
  --events '' \
  --now-ms "$NOW_MS" \
  --seed 416 \
  --quiet >/dev/null

python3 -m risk_governance.cli run \
  --workdir "$OUT/targeted" \
  --config data/generated/calibration_config.json \
  --events data/generated/change_events.jsonl \
  --now-ms "$NOW_MS" \
  --seed 416 \
  --quiet > "$OUT/targeted-cli.json"

python3 - <<'PY'
import json
r = json.load(open('out/acceptance/targeted/reports/execution_report.json'))
assert r['planned_recompute_set'] == ['B002', 'B003', 'B004', 'B006']
assert r['reused_set'] == ['B000', 'B001', 'B005', 'B007', 'B008']
assert r['coverage_check']['full_coverage']
assert r['event_closure_check']['event_closure_without_recompute'] == []
assert r['event_closure_check']['recompute_without_event_closure'] == []
assert r['cache_reference_chain']['valid']
print('targeted recompute: OK')
PY

echo '[4/7] repeated warm execution is byte-identical'
for _ in 1 2; do
  python3 -m risk_governance.cli run \
    --workdir "$OUT/determinism" \
    --events '' \
    --now-ms "$NOW_MS" \
    --seed 416 \
    --quiet >/dev/null
done
cp "$OUT/determinism/reports/execution_report.json" "$OUT/report.warm.json"
python3 -m risk_governance.cli run \
  --workdir "$OUT/determinism" \
  --events '' \
  --now-ms "$NOW_MS" \
  --seed 416 \
  --quiet >/dev/null
cmp "$OUT/report.warm.json" "$OUT/determinism/reports/execution_report.json"
python3 -m risk_governance.cli verify --workdir "$OUT/determinism" > "$OUT/verify-determinism.json"
echo 'byte-identical warm report: OK'

echo '[5/7] interrupt after two committed batches and restart'
set +e
python3 -m risk_governance.cli run \
  --workdir "$OUT/recovery" \
  --now-ms "$NOW_MS" \
  --seed 416 \
  --interrupt-after-batch 2 \
  --quiet > "$OUT/interrupted-cli.json"
interrupt_code=$?
set -e
test "$interrupt_code" -eq 143

python3 - <<'PY'
import json
s = json.load(open('out/acceptance/recovery/state/recovery.json'))
assert s['status'] == 'interrupted'
assert s['committed_batches'] == ['B000', 'B001']
assert s['committed_stage_index'] == 2
print('interrupt checkpoint: OK')
PY

python3 -m risk_governance.cli run \
  --workdir "$OUT/recovery" \
  --now-ms "$NOW_MS" \
  --seed 416 \
  --quiet >/dev/null

python3 - <<'PY'
import json
r = json.load(open('out/acceptance/recovery/reports/execution_report.json'))
assert r['recovery']['recovered']
assert r['recovered_skipped_batch_ids'] == ['B000', 'B001']
assert 'B002' in r['computed_batch_ids']
assert r['coverage_check']['full_coverage']
assert not list(__import__('pathlib').Path('out/acceptance/recovery/state/inflight').glob('*'))
assert not list(__import__('pathlib').Path('out/acceptance/recovery/cache/pending').glob('*'))
print('restart recovery: OK')
PY

echo '[6/7] old snapshot rollback is rejected'
set +e
python3 -m risk_governance.cli snapshot-rollback \
  --workdir "$OUT/targeted" \
  --to-version 0 > "$OUT/rollback.json"
rollback_code=$?
set -e
test "$rollback_code" -eq 3
grep -q E_SNAPSHOT_ROLLBACK "$OUT/rollback.json"
echo 'rollback rejection: OK'

echo '[7/8] final report verification'
python3 -m risk_governance.cli verify --workdir "$OUT/targeted" > "$OUT/verify-targeted.json"
python3 - <<'PY'
import json
for name in ('targeted', 'determinism', 'recovery'):
    report = json.load(open(f'out/acceptance/{name}/reports/execution_report.json'))
    stored = report.pop('checksum')
    from risk_governance.canonical import sha256_json
    assert stored == sha256_json(report), name
print('all report checksums: OK')
PY

echo '[8/8] replay report parameters into a new directory'
python3 -m risk_governance.cli replay \
  --report out/acceptance/targeted/reports/execution_report.json \
  --target-workdir out/acceptance/replay > "$OUT/replay.json"
grep -q '"match": true' "$OUT/replay.json"
echo 'replay stable hashes: OK'

echo 'ACCEPTANCE_OK'
