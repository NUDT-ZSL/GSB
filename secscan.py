#!/usr/bin/env python3
"""Convenience entry point: ``python secscan.py --config ...``."""

import sys
from pathlib import Path

# Keep imports local to this file's directory, then run the package CLI.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from secscan.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
