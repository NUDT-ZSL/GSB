#!/usr/bin/env python3
"""Convenience wrapper for the CLI run command."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anomaly_engine.cli import main


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main(["run", *sys.argv[1:]]))
