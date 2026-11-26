#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dataplane.seeddata import FIXED_SEED, generate_fixed_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local sample data")
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--seed", type=int, default=FIXED_SEED)
    args = parser.parse_args()
    summary = generate_fixed_samples(Path(args.data_dir), seed=args.seed)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
