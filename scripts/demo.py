#!/usr/bin/env python3
"""Stable local entry point for the repeatable demonstration."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from featurelab.cli import main  # noqa: E402


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0].startswith("-"):
        args = ["demo", *args]
    raise SystemExit(main(args))
