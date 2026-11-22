#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lease_arbiter.samplegen import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local JSON samples")
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    args = parser.parse_args()
    hashes = generate_samples(Path(args.out))
    print(json.dumps({"ok": True, "directory": str(Path(args.out)), "sha256": hashes}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
