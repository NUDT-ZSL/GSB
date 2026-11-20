#!/usr/bin/env python3
"""Generate deterministic local samples without network or external data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from mtstream.sample_data import make_sample_bundle


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/sample_bundle.json"),
    )
    args = parser.parse_args()
    bundle = make_sample_bundle(args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(args.out)


if __name__ == "__main__":
    main()

