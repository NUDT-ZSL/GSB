#!/usr/bin/env python3
"""Create the deterministic corpus and configuration fixtures."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from index_diag.samples import generate_samples


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="samples")
    parser.add_argument("--seed", type=int, default=20261127)
    args = parser.parse_args()
    manifest = generate_samples(args.output, seed=args.seed)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
