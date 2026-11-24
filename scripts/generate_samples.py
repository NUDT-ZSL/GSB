#!/usr/bin/env python3
"""Convenience wrapper for deterministic BSL sample generation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bsl_workbench.generator import main


if __name__ == "__main__":
    raise SystemExit(main())
