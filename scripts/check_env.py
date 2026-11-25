#!/usr/bin/env python3
from __future__ import annotations

import sqlite3
import sys


def main() -> int:
    try:
        import numpy as np
    except ImportError:
        print("numpy is required: pip install numpy", file=sys.stderr)
        return 2

    print(
        {
            "python": sys.version.split()[0],
            "numpy": np.__version__,
            "sqlite": sqlite3.sqlite_version,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
