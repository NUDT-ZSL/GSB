"""Regenerate the deterministic synthetic dataset (fixed seed)."""

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from ocr_workbench.samplegen import generate_dataset


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=os.path.join(ROOT, "samples"))
    parser.add_argument("--count", type=int, default=6)
    parser.add_argument("--seed", type=int, default=20261117)
    args = parser.parse_args()
    written = generate_dataset(args.out, image_count=args.count,
                               base_seed=args.seed)
    print(f"wrote {len(written)} deterministic PNG/JSON pairs to {args.out}")


if __name__ == "__main__":
    main()
