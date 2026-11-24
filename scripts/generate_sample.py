#!/usr/bin/env python3
"""Convenience wrapper for deterministic sample generation."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memguard.samplegen import SEED, generate_sample


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    truth = generate_sample(args.out, seed=args.seed)
    print(
        json.dumps(
            {
                "out": str(args.out),
                "seed": args.seed,
                "truth_records": len(truth["records"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
