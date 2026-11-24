#!/usr/bin/env python3
"""Convenience entry point: ``python generate_samples.py --output samples``."""

import sys

from balance_lab.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["generate-samples", *sys.argv[1:]]))
