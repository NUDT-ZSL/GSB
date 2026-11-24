#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anim_replay.samplegen import generate_samples  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local animation samples")
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=20261125)
    args = parser.parse_args()
    manifest = generate_samples(args.out, seed=args.seed)
    print(
        json.dumps(
            {
                "ok": True,
                "out": args.out,
                "seed": args.seed,
                "file_count": len(manifest["files"]),
                "version_base": manifest["summary"]["version_base"],
                "version_edited": manifest["summary"]["version_edited"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

