#!/usr/bin/env python3
"""Command-line wrapper for deterministic data generation."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intent_engine.data_generator import generate_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(ROOT / "data"))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    manifest = generate_dataset(args.out, args.seed)
    print(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

