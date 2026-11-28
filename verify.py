#!/usr/bin/env python3
"""Convenience wrapper for the acceptance-check command."""

from __future__ import annotations

import sys

from moe_capacity.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["verify", *sys.argv[1:]]))

