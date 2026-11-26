from __future__ import annotations

import argparse
from pathlib import Path

from diagnostic_engine.generator import (
    DEFAULT_SAMPLE_COUNT,
    DEFAULT_SEED,
    generate_dataset,
)
from diagnostic_engine.utils import pretty_json


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic JSONL samples and v1/v2 snapshots"
    )
    parser.add_argument("--out", default="data")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLE_COUNT)
    args = parser.parse_args()
    result = generate_dataset(
        Path(args.out), seed=args.seed, sample_count=args.samples
    )
    print(pretty_json(result.to_dict()), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

