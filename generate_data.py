#!/usr/bin/env python3
"""Generate deterministic local data for the workbench."""

from __future__ import annotations

import argparse
import json

from storybench.generate import DEFAULT_SEED, generate_workspace


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate deterministic story data")
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = parser.parse_args()
    manifest = generate_workspace(args.data_dir, args.seed)
    print(
        json.dumps(
            {
                "status": "generated",
                "data_dir": args.data_dir,
                "seed": manifest["generator"]["seed"],
                "story_count": manifest["story_count"],
                "level_count": manifest["level_count"],
            },
            indent=2,
            sort_keys=True,
        )
    )
