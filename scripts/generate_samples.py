#!/usr/bin/env python3
"""Convenience wrapper: python scripts/generate_samples.py --out samples."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tsaudit.generator import DEFAULT_SEED, generate_sample


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    print(json.dumps(generate_sample(args.out, args.seed), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
