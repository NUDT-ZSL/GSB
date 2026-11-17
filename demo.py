"""Convenience wrapper for `python -m infer_workbench demo`."""

from __future__ import annotations

import sys

from infer_workbench.cli import main


if __name__ == "__main__":
    raise SystemExit(main(["demo", *sys.argv[1:]]))
