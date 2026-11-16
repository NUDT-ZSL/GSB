#!/usr/bin/env python3
"""Generate deterministic local samples (thin wrapper around mmef.samplegen)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mmef.samplegen import SEED, generate  # noqa: E402

if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "data"
    generate(out, seed=SEED)
    print(f"samples generated at {out}")
