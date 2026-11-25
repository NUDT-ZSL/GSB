#!/usr/bin/env python3
"""Convenience wrapper: generate the fixed-seed local sample."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from diagnostic_engine.generator import generate_fixture


if __name__ == "__main__":
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "samples"
    paths = generate_fixture(output)
    for name, path in paths.items():
        print(f"{name}: {path}")
