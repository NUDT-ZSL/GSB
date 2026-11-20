#!/usr/bin/env python3
"""Generate the local deterministic JSONL sample set."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anomaly_engine.dataset import generate_dataset_file


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="data/demo.jsonl")
    parser.add_argument("--seed", type=int, default=20261121)
    parser.add_argument("--samples", type=int, default=600)
    parser.add_argument("--clusters", type=int, default=6)
    parser.add_argument("--features", type=int, default=5)
    parser.add_argument("--anomaly-ratio", type=float, default=0.08)
    args = parser.parse_args()
    manifest = generate_dataset_file(
        args.output,
        seed=args.seed,
        n_total=args.samples,
        n_clusters=args.clusters,
        n_features=args.features,
        anomaly_ratio=args.anomaly_ratio,
    )
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
