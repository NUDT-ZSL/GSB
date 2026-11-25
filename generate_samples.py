#!/usr/bin/env python3
"""Root compatibility wrapper for the fixed-seed sample generator."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gradient_engine.generator import DEFAULT_SEED, generate_samples


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("samples")
    seed = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_SEED
    print(generate_samples(out, seed=seed))
