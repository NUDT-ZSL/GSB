#!/usr/bin/env python3
"""Run the full deterministic drift-AE example.

Examples:
  python examples/end_to_end_demo.py --fresh
  python examples/end_to_end_demo.py --state-dir artifacts/recovery --crash-after 3
  python examples/end_to_end_demo.py --state-dir artifacts/recovery
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from drift_ae.demo import main


if __name__ == "__main__":
    raise SystemExit(main())
