#!/usr/bin/env python3
"""Convenience entry point: run the three-scenario demo by default."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shardrouter.cli import main  # noqa: E402


if __name__ == "__main__":
    arguments = sys.argv[1:]
    if not arguments or arguments[0].startswith("-"):
        arguments = ["demo", *arguments]
    raise SystemExit(main(arguments))
