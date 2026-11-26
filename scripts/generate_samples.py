#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from kvledger.seed import generate_samples


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "samples"
    print(generate_samples(out))
