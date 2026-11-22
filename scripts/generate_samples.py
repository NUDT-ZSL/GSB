#!/usr/bin/env python3
"""Generate deterministic local CSR samples without external data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sparse_inc.samples import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=20261123)
    args = parser.parse_args()
    paths = generate_samples(Path(args.out), seed=args.seed)
    print(json.dumps(paths, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
