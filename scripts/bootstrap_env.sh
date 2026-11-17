#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)'; then
  echo "Python 3.11 is required" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  python3 -m venv --without-pip .venv
fi

SITE=".venv/lib/python3.11/site-packages"
mkdir -p "$SITE"

if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  PIP_WHEEL="${PIP_WHEEL:-$(find /tmp/pylocal /usr/share/python-wheels -name 'pip-*.whl' 2>/dev/null | head -n 1)}"
  if [ -z "$PIP_WHEEL" ]; then
    echo "No local pip wheel found; set PIP_WHEEL=/path/to/pip.whl" >&2
    exit 1
  fi
  .venv/bin/python - "$PIP_WHEEL" "$SITE" <<'PY'
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
  cat > .venv/bin/pip <<EOF
#!$ROOT/.venv/bin/python
import sys
from pip._internal.cli.main import main
if __name__ == "__main__":
    sys.exit(main())
EOF
  chmod +x .venv/bin/pip
fi

if ! .venv/bin/python -c 'import numpy' >/dev/null 2>&1; then
  NUMPY_WHEEL="${NUMPY_WHEEL:-$(find /tmp/wheelout -maxdepth 1 -name 'numpy-*cp311*.whl' 2>/dev/null | head -n 1)}"
  if [ -z "$NUMPY_WHEEL" ]; then
    NUMPY_WHEEL="$(find /tmp -maxdepth 5 -name 'numpy-*cp311*.whl' 2>/dev/null | head -n 1)"
  fi
  if [ -z "$NUMPY_WHEEL" ]; then
    echo "No local cp311 NumPy wheel found; set NUMPY_WHEEL=/path/to/numpy.whl" >&2
    exit 1
  fi
  .venv/bin/python -m pip install --no-index --no-deps "$NUMPY_WHEEL"
fi

mkdir -p "$HOME/.local/bin"
python3 - "$HOME/.local/bin/python" "$HOME/.local/bin/pip" <<'PY'
import sys
from pathlib import Path
for value in sys.argv[1:]:
    Path(value).unlink(missing_ok=True)
PY
cat > "$HOME/.local/bin/python" <<EOF
#!/bin/sh
exec "$ROOT/.venv/bin/python" "\$@"
EOF
cat > "$HOME/.local/bin/pip" <<EOF
#!/bin/sh
exec "$ROOT/.venv/bin/pip" "\$@"
EOF
chmod +x "$HOME/.local/bin/python" "$HOME/.local/bin/pip"

python --version
pip --version
python -c 'import numpy; print("NumPy", numpy.__version__)'
