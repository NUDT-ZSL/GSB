#!/usr/bin/env bash
# Reproducible end-to-end demonstration. No network, GPU or external service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY=".venv/bin/python"

echo "== [0/6] environment =="
$PY -c "import sys, numpy, scipy; print(sys.version.split()[0], 'numpy', numpy.__version__, 'scipy', scipy.__version__)"

echo "== [1/6] deterministic sample generation (seed=20261120) =="
$PY -m prank.cli generate --out-dir data/sample --seed 20261120 \
    --queries 40 --min-items 60 --max-items 120

echo "== [2/6] full three-tier baseline run =="
$PY -m prank.cli run --data-dir data/sample --config config/default.json \
    --report reports/baseline.json

echo "== [3/6] rerun and report-hash comparison =="
$PY -m prank.cli run --data-dir data/sample --config config/default.json \
    --report reports/baseline_rerun.json
$PY -m prank.cli hash-check --reports reports/baseline.json reports/baseline_rerun.json

echo "== [4/6] multi-point fault injection (dup query / empty terms / range) =="
# 4a: dataset-level fatal error (duplicate query) -> E_DUP_QUERY, portable exit code
$PY -m prank.cli run --data-dir data/sample \
    --config config/default.json --report reports/injection_dup.json \
    --injections config/injections.dup.json \
    --error-report reports/injection_dup_fatal.json || true
# 4b: multi-point per-query injections with explicit degradation chain
$PY -m prank.cli run --data-dir data/sample \
    --config config/default.json --report reports/injection.json \
    --injections config/injections.multipoint.json

echo "== [4b/6] runtime parameter injection (unknown precision / bad scale) =="
$PY -m prank.cli run --data-dir data/sample \
    --config config/default.json --report reports/injection_runtime.json \
    --injections config/injections.runtime.json || true

echo "== [5/6] degradation demonstration (per-query fallback to float64) =="
$PY -m prank.cli run --data-dir data/sample --config config/default.json \
    --report reports/degradation.json \
    --injections config/injections.degrade.json

echo "== [6/6] versioned snapshots (wholesale replace + checksum) =="
$PY -m prank.cli snapshot --snapshot-action submit-config \
    --config config/default.json --path snapshots/config.json
$PY -m prank.cli snapshot --snapshot-action submit-stats \
    --report reports/baseline.json --path snapshots/statistics.json
$PY -m prank.cli snapshot --snapshot-action load \
    --path snapshots/statistics.json --category statistics

echo "All demo stages finished. Reports are under reports/."
