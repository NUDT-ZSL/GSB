#!/usr/bin/env python3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from seed_samples import generate  # noqa: E402


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "samples"
    print(generate(target))
