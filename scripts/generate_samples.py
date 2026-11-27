#!/usr/bin/env python3
"""Generate the deterministic project and all four boundary samples."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from weld_train.generator import DEFAULT_SEED, generate_project


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    result = generate_project(args.data_dir, seed=args.seed)
    print(f"generated: {args.data_dir}")
    for item in result["outputs"]:
        print(
            f"  {item['segment_id']}: snapshot {item['snapshot_version']} "
            f"audio {item['audio_sha256'][:16]}..."
        )
    print(f"validation pass: {result['validation']['pass']}")
    return 0 if result["validation"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
