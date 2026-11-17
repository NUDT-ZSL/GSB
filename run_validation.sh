#!/usr/bin/env bash
# Reproducible acceptance validation for the image-text evidence pipeline.
# Each invocation uses a fresh, run-stamped work directory so the script can
# be rerun safely; identical inputs still produce byte-identical reports.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

RUN_ID="$(date -u +%Y%m%dT%H%M%S)-$$"
DATA="data/samples"
WORK="work/validation-$RUN_ID"
ART="artifacts/validation"
mkdir -p "$WORK" "$ART"

echo "== 1. unit + integration tests =="
python3 -m unittest discover -s tests

echo "== 2. deterministic sample generation =="
python3 -m pipeline.cli generate --input-dir "$DATA" \
    --config configs/fusion_config.json --samples 8 \
    --manifest "$ART/manifest.json"

echo "== 3. serial baseline (concurrency=1) =="
python3 -m pipeline.cli run --input-dir "$DATA" \
    --config configs/fusion_config_serial.json --relations configs/relations.json \
    --injections configs/injections_empty.json \
    --work-dir "$WORK/serial" --report "$ART/baseline_serial.json"

echo "== 4. parallel run (concurrency=3) =="
python3 -m pipeline.cli run --input-dir "$DATA" \
    --config configs/fusion_config.json --relations configs/relations.json \
    --injections configs/injections_empty.json \
    --work-dir "$WORK/parallel" --report "$ART/baseline_parallel.json"
cmp "$ART/baseline_serial.json" "$ART/baseline_parallel.json"
echo "  concurrency byte-identical: OK"

echo "== 5. interrupted run then resume =="
PIPELINE_INTERRUPT_AFTER_ROUND=1 python3 -m pipeline.cli run --input-dir "$DATA" \
    --config configs/fusion_config.json --relations configs/relations.json \
    --injections configs/injections.json \
    --work-dir "$WORK/resume" --report "$WORK/never-written.json"
test ! -e "$WORK/never-written.json"
python3 -m pipeline.cli run --input-dir "$DATA" \
    --config configs/fusion_config.json --relations configs/relations.json \
    --injections configs/injections.json \
    --work-dir "$WORK/resume" --report "$ART/resumed.json"
python3 -m pipeline.cli run --input-dir "$DATA" \
    --config configs/fusion_config.json --relations configs/relations.json \
    --injections configs/injections.json \
    --work-dir "$WORK/full" --report "$ART/full_injected.json"
cmp "$ART/resumed.json" "$ART/full_injected.json"
echo "  resume byte-identical: OK"

echo "== 6. quantization/temperature variant =="
python3 -m pipeline.cli run --input-dir "$DATA" \
    --config configs/fusion_config_quant4_temp05.json \
    --relations configs/relations.json --injections configs/injections_empty.json \
    --work-dir "$WORK/variant" --report "$ART/variant.json"

echo "== 7. hashes =="
sha256sum "$ART"/baseline_serial.json "$ART"/baseline_parallel.json \
    "$ART"/resumed.json "$ART"/full_injected.json "$ART"/variant.json \
    "$ART"/manifest.json | tee "$ART/SHA256SUMS"
echo "ALL VALIDATION STEPS PASSED (work=$WORK)"
