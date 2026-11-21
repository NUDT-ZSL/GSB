#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from deploylab.bundle import bundle_summary, write_bundle


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic local model bundles")
    parser.add_argument("--out", default=str(ROOT / "data" / "sample"))
    parser.add_argument("--calib-samples", type=int, default=8)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    bundle = write_bundle(
        args.out,
        calib_samples=args.calib_samples,
        force=args.force,
    )
    print(json.dumps(bundle_summary(bundle), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

