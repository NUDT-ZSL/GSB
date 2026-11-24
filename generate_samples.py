#!/usr/bin/env python3
"""Write the fixed-seed local samples."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memguard.generator import generate_fixture


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out-dir", type=Path, default=Path("sample"))
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()
    paths = generate_fixture(args.out_dir, args.seed)
    print(json.dumps({"status": "ok", "files": paths}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
