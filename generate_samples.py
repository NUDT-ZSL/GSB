#!/usr/bin/env python3
"""Fixed-seed entry point: ``python generate_samples.py``."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from proxy_diag.samplegen import main


if __name__ == "__main__":
    raise SystemExit(main())
