#!/usr/bin/env python3
"""Compatibility entry point: scripts/generate_seed.py."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from generate_samples import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
