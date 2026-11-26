#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fusion_diag.generate import SEED, generate_workspace  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate the fixed-seed local fusion diagnostics samples"
    )
    parser.add_argument("--out", default="samples")
    parser.add_argument("--seed", type=int, default=SEED)
    args = parser.parse_args()
    manifest = generate_workspace(args.out, seed=args.seed)
    print(
        json.dumps(
            manifest,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
