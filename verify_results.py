#!/usr/bin/env python3
"""Standalone verification wrapper."""

import sys

from coref_pipeline.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["verify", *sys.argv[1:]]))
