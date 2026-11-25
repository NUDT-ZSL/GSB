#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from feature_diag.generator import prepare_workspace
from feature_diag.util import canonical_json


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--seed", type=int, default=20261126)
    parser.add_argument("--count", type=int, default=32)
    args = parser.parse_args()
    result = prepare_workspace(args.root, seed=args.seed, count=args.count)
    sys.stdout.write(canonical_json({"status": "ok", "workspace": result}, newline=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
