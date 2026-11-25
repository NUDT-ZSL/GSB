#!/usr/bin/env python3
"""Fixed-seed generator for windows, v1/v2 thresholds and drift events."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drift_ae.fixtures import main


if __name__ == "__main__":
    raise SystemExit(main())
