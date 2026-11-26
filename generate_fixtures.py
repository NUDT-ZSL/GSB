#!/usr/bin/env python3
"""Fixed-seed fixture generator for the crash-consistency engine."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from diagnostic_engine.seed import FIXED_SEED, generate_fixtures
from diagnostic_engine.validation import load_fixtures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="fixtures/seed")
    parser.add_argument("--seed", type=int, default=FIXED_SEED)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    path = generate_fixtures(args.output, seed=args.seed, clean=not args.no_clean)
    bundle = load_fixtures(path)
    print(json.dumps({
        "ok": True,
        "output": str(path),
        "seed": bundle.seed,
        "objects": len(bundle.objects),
        "events": len(bundle.events),
        "injections": len(bundle.injections),
    }, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
