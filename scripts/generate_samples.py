#!/usr/bin/env python3
"""Create all deterministic sample inputs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ragdiag.sample_generator import generate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data", help="output directory")
    args = parser.parse_args()
    manifest = generate(args.out)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

