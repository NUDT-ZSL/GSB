#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required to install Python 3.11 in this workspace" >&2
  exit 1
fi

uv python install 3.11.18
uv venv --python 3.11.18 .venv
uv pip install --python .venv/bin/python -e .

mkdir -p .tools/downloads .tools/root .tools/bin
if [ ! -x .tools/root/usr/bin/sqlite3 ]; then
  (
    cd .tools/downloads
    apt-get download sqlite3
    dpkg-deb -x "$(ls -t sqlite3_*.deb | head -n 1)" ../root
  )
fi

chmod +x .tools/bin/sqlite3 .tools/bin/python scripts/run_validation.py
.venv/bin/python --version
.tools/bin/sqlite3 --version

