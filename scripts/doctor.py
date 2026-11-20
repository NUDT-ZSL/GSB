#!/usr/bin/env python3
from __future__ import annotations

import json
import sqlite3
import sys


def main() -> int:
    try:
        import numpy
    except ImportError as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "python": sys.version.split()[0],
                    "error": f"numpy unavailable: {exc}",
                },
                sort_keys=True,
            )
        )
        return 1
    ok = sys.version_info >= (3, 11)
    print(
        json.dumps(
            {
                "ok": ok,
                "python": sys.version.split()[0],
                "python_311_or_newer": ok,
                "numpy": numpy.__version__,
                "sqlite3": sqlite3.sqlite_version,
                "executable": sys.executable,
            },
            sort_keys=True,
        )
    )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

