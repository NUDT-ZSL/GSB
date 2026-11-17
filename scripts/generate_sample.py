"""Generate deterministic local sample volumes from the snapshot plan.

Usage (Windows)::

    python scripts\\generate_sample.py --plan samples\\snapshot_plan.json --out demo_workspace

No network access, no external accounts; every parameter is an integer or
hex string.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blocksnap.volume import Volume  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="generate deterministic sample volume")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--name", default="sample_volume.bin")
    args = parser.parse_args()

    with open(args.plan, "r", encoding="utf-8") as handle:
        plan = json.load(handle)
    os.makedirs(args.out, exist_ok=True)
    image_path = os.path.join(args.out, args.name)
    volume = Volume.create(
        image_path, int(plan["block_size"]), int(plan["block_count"])
    )
    result = {
        "image": volume.image_path,
        "block_size": volume.block_size,
        "block_count": volume.block_count,
        "length": volume.block_size * volume.block_count,
        "image_digest": volume.current_digest(),
    }
    volume.close()
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
