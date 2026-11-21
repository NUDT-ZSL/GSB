#!/usr/bin/env python3
"""One-command launcher for the LockFair interactive demo."""

from __future__ import annotations

import sys

from lockfair.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
