#!/usr/bin/env python3
"""Generate the deterministic local sample corpus."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meeting_vad.synth import generate_samples


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(ROOT / "config" / "synthesis.json"),
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "samples"),
    )
    parser.add_argument("--keep", action="store_true", help="do not delete output first")
    args = parser.parse_args()
    summary = generate_samples(args.config, args.output, clean=not args.keep)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
