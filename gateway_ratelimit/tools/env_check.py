"""Verify the local runtime: Python version, sqlite3, optional numpy/tkinter."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    print("python:", sys.version.replace("\n", " "))
    if sys.version_info < (3, 11):
        print("FAIL: Python 3.11+ required")
        return 1
    import sqlite3

    print("sqlite3:", sqlite3.sqlite_version)
    try:
        import numpy

        print("numpy:", numpy.__version__, "(optional acceleration available)")
    except Exception as exc:
        print("numpy: unavailable (", exc, ") - pure stdlib fallback used")
    try:
        import tkinter

        print("tkinter:", tkinter.TkVersion, "(optional visualisation available)")
    except Exception as exc:
        print("tkinter: unavailable (", exc, ") - headless text view used instead")
    print("result: core requirements OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
