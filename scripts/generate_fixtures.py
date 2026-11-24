#!/usr/bin/env python3
"""Standalone fixed-seed fixture generator."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from audio_pipeline.seed import generate_fixtures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("demo"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    result = generate_fixtures(args.workspace, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
