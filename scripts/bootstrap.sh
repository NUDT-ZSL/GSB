#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON_BIN="${PYTHON_BIN:-python3}"

"$PYTHON_BIN" - <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("Python 3.11 or newer is required")
PY

if [[ ! -d "$ROOT/.venv" ]]; then
  "$PYTHON_BIN" -m venv --system-site-packages "$ROOT/.venv"
fi

VENV_PYTHON="$ROOT/.venv/bin/python"
"$VENV_PYTHON" -c "import numpy, pytest"

WHEEL_DIR="${WHEEL_DIR:-$ROOT/wheels}"
if "$VENV_PYTHON" -c "import setuptools" >/dev/null 2>&1; then
  if [[ -d "$WHEEL_DIR" ]] && compgen -G "$WHEEL_DIR/*.whl" >/dev/null; then
    "$VENV_PYTHON" -m pip install --no-index --find-links "$WHEEL_DIR" -e "$ROOT[test]"
  else
    "$VENV_PYTHON" -m pip install --no-index --no-deps --no-build-isolation -e "$ROOT"
  fi
else
  SITE_DIR="$("$VENV_PYTHON" -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')"
  printf '%s\n' "$ROOT" > "$SITE_DIR/ic_ledger_project.pth"
  if [[ ! -e "$ROOT/.venv/bin/ic-ledger" ]]; then
    {
      printf '#!%s\n' "$VENV_PYTHON"
      printf '%s\n' 'from ic_ledger.cli import main'
      printf '%s\n' 'raise SystemExit(main())'
    } > "$ROOT/.venv/bin/ic-ledger"
    chmod +x "$ROOT/.venv/bin/ic-ledger"
  fi
fi

echo "environment ready: $ROOT/.venv/bin/python"
