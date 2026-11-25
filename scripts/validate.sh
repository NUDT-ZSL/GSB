#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

python3 -m audio_qc.cli generate --out samples
python3 -m audio_qc.cli acceptance --workdir artifacts/acceptance
python3 -m audio_qc.cli verify --sample-dir samples --report artifacts/acceptance/recovered-report.json
python3 -m pytest -q
