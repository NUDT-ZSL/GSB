#!/usr/bin/env python3
"""Run or verify the local graded-story workbench."""

from __future__ import annotations

import sys

from storybench.cli import main


if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv:
        argv = ["run"]
    raise SystemExit(main(argv))
