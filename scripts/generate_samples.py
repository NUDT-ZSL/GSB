#!/usr/bin/env python3
"""Generate deterministic AVIs plus per-frame ground truth."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from video_pipeline.samples import generate_sample_corpus


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="samples")
    parser.add_argument("--ground-truth", default="samples/ground_truth.json")
    args = parser.parse_args()
    truth = generate_sample_corpus(args.output, args.ground_truth)
    print(f"generated {len(truth['routes'])} routes under {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
