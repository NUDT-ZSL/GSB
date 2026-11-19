#!/usr/bin/env python3
"""Generate deterministic multilingual samples and alignment resources."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xlpipe.sample_gen import generate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="output data directory")
    parser.add_argument("--resources", default="resources",
                        help="output resources directory")
    parser.add_argument("--pairs", type=int, default=120)
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--resource-version", type=int, default=1)
    args = parser.parse_args()
    info = generate(args.data, args.resources, n_pairs=args.pairs,
                    seed=args.seed, resource_version=args.resource_version)
    print(json.dumps({"status": "generated", **info}, ensure_ascii=False))


if __name__ == "__main__":
    main()
