#!/usr/bin/env bash
# Reproducible baseline: generate samples, run pipeline twice, compare hashes.
set -euo pipefail
cd "$(dirname "$0")/.."
SEED=20261120

python3 -m chart_reader generate --out samples --seed "$SEED" --count 6
python3 -m chart_reader run --samples samples --config config/config.example.json \
    --out reports/run_a.json --snapshots snapshots
python3 -m chart_reader run --samples samples --config config/config.example.json \
    --out reports/run_b.json --snapshots snapshots
python3 -m chart_reader verify --report reports/run_a.json
python3 -m chart_reader verify --report reports/run_b.json
cmp reports/run_a.json reports/run_b.json && echo "REPRODUCIBLE: reports byte-identical"
