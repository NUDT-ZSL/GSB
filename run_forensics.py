"""Source-tree launcher that does not require installation."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from config_forensics.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
