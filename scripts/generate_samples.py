#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from elastic_control.samplegen import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic sample JSON files")
    parser.add_argument("--out", default=str(Path("samples")), help="output directory")
    args = parser.parse_args()
    for path in generate_samples(args.out):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
