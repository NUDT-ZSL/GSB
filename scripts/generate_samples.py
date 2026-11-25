#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from latent_diag.generator import FIXED_GENERATOR_SEED, generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate fixed-seed diagnostic samples")
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=FIXED_GENERATOR_SEED)
    args = parser.parse_args()
    metadata = generate_samples(args.out, seed=args.seed)
    print(json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

