#!/usr/bin/env python3
"""Fail fast when the local deterministic runtime prerequisites are unavailable."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import sys

import numpy as np


def main() -> int:
    payload = {
        "python": sys.version.split()[0],
        "sqlite3": sqlite3.sqlite_version,
        "numpy": np.__version__,
        "multiprocessing_start_method": multiprocessing.get_start_method(),
        "ok": True,
    }
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
