#!/usr/bin/env bash
# Deterministic end-to-end VAD demo: generate, run, test.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH=src
python3.11 scripts/generate_samples.py
python3.11 -m vad.cli --force-restart
python3.11 -m unittest tests.test_vad -v
