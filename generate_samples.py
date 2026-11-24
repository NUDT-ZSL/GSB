#!/usr/bin/env python3
from __future__ import annotations

import argparse

from screen_diag.sample_generator import SEED, generate_sample_tree


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local screening samples")
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    generate_sample_tree(args.out, seed=args.seed)
    print(f"generated deterministic samples at {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
