#!/usr/bin/env python3
"""Compatibility wrapper for the facial-animation pipeline."""

import sys

from face_anim.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["pipeline", *sys.argv[1:]]))
