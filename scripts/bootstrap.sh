#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to install the pinned local Python toolchain." >&2
  exit 2
fi

uv python install 3.12
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python pip
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python tools/generate_samples.py
.venv/bin/python -c 'import sys, sqlite3, numpy; print(f"python={sys.version.split()[0]} numpy={numpy.__version__} sqlite={sqlite3.sqlite_version}")'
