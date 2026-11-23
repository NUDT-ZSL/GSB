#!/usr/bin/env python3
"""Generate the deterministic local dataset without network access."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support_summary.sample_data import DEFAULT_SEED, write_dataset


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    write_dataset(args.root, force=args.force, seed=args.seed)
    print(f"dataset ready under {args.root / 'data'} (seed={args.seed})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
