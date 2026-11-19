#!/usr/bin/env bash
set -euo pipefail

PYTHON="${PYTHON:-python}"

"${PYTHON}" -m multilabel_lab.cli classify \
  --state-dir artifacts/snapshots/demo-normal \
  --samples 8 \
  --output artifacts/reports/normal.json

"${PYTHON}" -m multilabel_lab.cli inject \
  --state-dir artifacts/snapshots/demo-injection \
  --samples 8 \
  --injections examples/injections_multi.json \
  --output artifacts/reports/injection_multi.json

"${PYTHON}" -m multilabel_lab.cli classify \
  --state-dir artifacts/snapshots/demo-resource \
  --samples 12 \
  --max-samples 5 \
  --sample-policy truncate \
  --output artifacts/reports/resource_degraded.json

"${PYTHON}" -m multilabel_lab.cli verify \
  --state-dir artifacts/snapshots/demo-normal \
  --samples 8 \
  --output artifacts/reports/verification.json

"${PYTHON}" -m multilabel_lab.cli metrics \
  --report artifacts/reports/injection_multi.json \
  --output artifacts/reports/metrics_recompute.json
