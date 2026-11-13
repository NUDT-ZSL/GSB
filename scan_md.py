#!/usr/bin/env python3
"""Standalone launcher: ``python scan_md.py --dir ... --config ... --output ...``

``sys.path`` is set explicitly to this file's directory instead of relying on
the process working directory, so the scanner can be run from anywhere and
can never pick up a module planted in the folder being scanned.  For the same
reason the tests invoke it with ``python -I`` (isolated mode).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mdscan.cli import entrypoint  # noqa: E402

if __name__ == "__main__":
    sys.exit(entrypoint())
