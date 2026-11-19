#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from h2diag.sample import write_sample_scenario


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output")
    parser.add_argument("--seed", type=int, default=20261120)
    args = parser.parse_args()
    write_sample_scenario(args.output, args.seed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

