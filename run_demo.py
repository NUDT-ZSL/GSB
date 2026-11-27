#!/usr/bin/env python3
"""Run the complete deterministic local example with relative paths."""

from __future__ import annotations

import sys

from signhand.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["run", *sys.argv[1:]]))

