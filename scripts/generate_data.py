#!/usr/bin/env python3
"""Generate the fixed-seed sample under data/."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from telemetry_drift.samplegen import generate_dataset


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data")
    args = parser.parse_args()
    Path(args.out).mkdir(parents=True, exist_ok=True)
    manifest = generate_dataset(args.out)
    print(
        f"generated {manifest['record_count']} records with seed "
        f"{manifest['seed']} in {args.out}"
    )


if __name__ == "__main__":
    main()

