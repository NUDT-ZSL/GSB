#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seg_diag.sample_generator import DEFAULT_SEED, generate_sample_directory


if __name__ == "__main__":
    output = ROOT / "samples" / f"local_seed_{DEFAULT_SEED}"
    generate_sample_directory(output, force="--force" in sys.argv)
    print(output)
