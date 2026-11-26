#!/usr/bin/env python3
"""Generate the fixed-seed meeting PCM/WAV/manifest bundle.

Run from the repository root:
    python scripts/generate_deterministic_sample.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from meeting_audit.sample_generator import FIXED_SEED, write_sample_bundle  # noqa: E402


def main() -> int:
    output = ROOT / "data" / "sample"
    paths = write_sample_bundle(output)
    print(f"fixed_seed={FIXED_SEED}")
    for name, path in paths.items():
        print(f"{name}: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
