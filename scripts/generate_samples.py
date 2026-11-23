#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from graphstore.samples import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic graph samples")
    parser.add_argument("--out", default="samples", help="destination directory")
    parser.add_argument("--force", action="store_true", help="replace existing directory")
    args = parser.parse_args()
    result = generate_samples(Path(args.out), force=args.force)
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

