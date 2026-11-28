#!/usr/bin/env python3
"""Compatibility wrapper for deterministic sample generation."""

import sys

from face_anim.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["generate", *sys.argv[1:]]))
