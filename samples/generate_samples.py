#!/usr/bin/env python3
"""Fixed-seed sample generator entry point.

Running this file regenerates the four deterministic input files in ``samples/``.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gradient_engine.generator import DEFAULT_SEED, generate_samples  # noqa: E402


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples"
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SEED
    print(generate_samples(out, seed=seed))
