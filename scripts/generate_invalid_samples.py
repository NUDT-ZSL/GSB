#!/usr/bin/env python3
"""Create invalid-header and truncated-AVI samples for failure injection."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_pipeline.samples import create_invalid_samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="invalid_samples")
    args = parser.parse_args()
    paths = create_invalid_samples(args.output)
    print("\n".join(str(path) for path in paths))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
