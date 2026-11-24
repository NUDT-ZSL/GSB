#!/usr/bin/env python3
"""Root-level convenience wrapper for digest/input verification."""

import sys

from surface_stitch.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["verify", *sys.argv[1:]]))
