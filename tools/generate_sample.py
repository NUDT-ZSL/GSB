#!/usr/bin/env python3
"""Generate the fixed-seed offline sample."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alert_engine.sample_gen import DEFAULT_SEED, generate_sample


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("samples"),
        help="sample output directory",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    truth = generate_sample(args.out, seed=args.seed)
    print(
        f"generated {args.out} seed={truth['seed']} "
        f"observation_clock_ms={truth['observation_clock_ms']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
