#!/usr/bin/env sh
set -eu

PYTHON_BIN="${PYTHON_BIN:-python3}"
WHEEL_HOUSE="${WHEEL_HOUSE:-wheels}"

rm -rf .venv
"$PYTHON_BIN" -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check --no-index \
  --find-links "$WHEEL_HOUSE" -r requirements.txt
.venv/bin/python -c "import numpy, PIL; print('numpy', numpy.__version__, 'Pillow', PIL.__version__)"
