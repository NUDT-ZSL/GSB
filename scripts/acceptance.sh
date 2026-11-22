#!/usr/bin/env bash
# End-to-end acceptance: deterministic samples -> full run -> incremental
# commits (weight / calib) -> consistency verification -> staleness checks.
set -euo pipefail
cd "$(dirname "$0")/.."

PY=${PYTHON:-python3}
CLI="-m quant_conv_pipeline.cli"
CFG=config.json

echo "== [1/8] reset workspace =="
$PY - <<'EOF'
import shutil
for d in ("data", "run"):
    shutil.rmtree(d, ignore_errors=True)
EOF

echo "== [2/8] generate deterministic samples =="
$PY $CLI gen-samples --out data

echo "== [3/8] full run =="
$PY $CLI run --config $CFG --data data | $PY -c "
import json,sys; r=json.load(sys.stdin)
print('snapshot:', r['snapshot_id'], '| jobs:', len(r['jobs']),
      '| tolerance_failures:', len(r['tolerance_failures']),
      '| validation_errors:', len(r['validation_errors']))"

echo "== [4/8] incremental commit: weight quant params v2 =="
$PY $CLI commit --config $CFG --updates data/updates/weight_params_v2.jsonl \
    --kind weight | $PY -c "
import json,sys; r=json.load(sys.stdin); inv=r['invalidation']
print('snapshot:', r['snapshot_id'], '| nodes:', inv['affected_nodes'])
print('  affected_jobs:', inv['affected_jobs'])
print('  recompute:', sorted((e['node'],e['job_id']) for e in inv['recompute']))
print('  reuse    :', sorted((e['node'],e['job_id']) for e in inv['reuse']))"

echo "== [5/8] incremental commit: calib stats v2 =="
$PY $CLI commit --config $CFG --updates data/updates/calib_stats_v2.jsonl \
    --kind calib | $PY -c "
import json,sys; r=json.load(sys.stdin); inv=r['invalidation']
print('snapshot:', r['snapshot_id'], '| nodes:', inv['affected_nodes'])
print('  affected_jobs:', inv['affected_jobs'])
print('  recompute:', sorted((e['node'],e['job_id']) for e in inv['recompute']))
print('  reuse    :', sorted((e['node'],e['job_id']) for e in inv['reuse']))"

echo "== [6/8] consistency self-check =="
for s in snap-0001 snap-0002 snap-0003; do
  $PY $CLI verify --config $CFG --snapshot $s | $PY -c "
import json,sys; r=json.load(sys.stdin)
print(r['snapshot_id'], '| consistent:', r['consistent'],
      '| replayed_ok:', r['entries_replayed_ok'])"
done

echo "== [7/8] staleness & lineage =="
$PY $CLI check-result --config $CFG --snapshot snap-0001 --job job-002 \
  | $PY -c "
import json,sys; r=json.load(sys.stdin)
print('stale check:', r.get('code'), '| job:', r.get('job_id'),
      '| diff:', r.get('details',{}).get('version_diff'))" || true
$PY $CLI check-result --config $CFG --snapshot snap-0003 --job job-007 \
  | $PY -c "
import json,sys; r=json.load(sys.stdin)
print('fresh check:', r['status'], '| replayed:', r['replayed'],
      '| stale:', r['stale'])"

echo "== [8/8] deviation spectrum (snap-0002) =="
$PY $CLI report --config $CFG --snapshot snap-0002 | $PY -c "
import json,sys; r=json.load(sys.stdin)
for jid, s in sorted(r['deviation_spectrum'].items()):
    lv = s['levels']
    print(jid, 'dominant:', s['dominant_error_source'],
          '| L1 %.4f L2 %.4f L3 %.4f L4 %.4f' % (
              lv['quant_input']['max_abs'], lv['fixed_point_mac']['max_abs'],
              lv['accumulator_reduce']['max_abs'],
              lv['dequant_output']['max_abs']))"

echo "== unit tests =="
$PY -m pytest tests/ -q
echo "ACCEPTANCE OK"
