#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${ROOT}/.venv/bin/python"
if [[ ! -x "${PY}" ]]; then
  echo "Missing ${PY}; create .venv and install requirements.txt first." >&2
  exit 1
fi
cd "${ROOT}"
"${PY}" -m unittest discover -s tests -v
"${PY}" -m convedit demo --output artifacts/demo
