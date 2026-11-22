#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

.venv/bin/python -m log_retention.sample_data --root sample
./retention --config sample/config.json load
./retention --config sample/config.json merge

set +e
RETENTION_FAULT_JUDGE_AFTER_STAGED=1 \
  ./retention --config sample/config.json judge --instance replica-a
JUDGE_RC=$?
set -e
if [[ "$JUDGE_RC" -ne 99 ]]; then
  echo "expected injected judge crash exit 99, got $JUDGE_RC" >&2
  exit 1
fi
./retention --config sample/config.json resume
./retention --config sample/config.json status

set +e
RETENTION_FAULT_REBUILD_AFTER_STAGED=1 \
  ./retention --config sample/config.json rebuild
REBUILD_RC=$?
set -e
if [[ "$REBUILD_RC" -ne 99 ]]; then
  echo "expected injected rebuild crash exit 99, got $REBUILD_RC" >&2
  exit 1
fi
./retention --config sample/config.json resume
./retention --config sample/config.json verify

