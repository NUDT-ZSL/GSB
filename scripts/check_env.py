#!/usr/bin/env python3
"""Emit a deterministic local runtime readiness check."""

from __future__ import annotations

import json
import sqlite3
import sys

import numpy


print(
    json.dumps(
        {
            "python": sys.version.split()[0],
            "python_executable": sys.executable,
            "numpy": numpy.__version__,
            "sqlite": sqlite3.sqlite_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
)

