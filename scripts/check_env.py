#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--compact", action="store_true")
    args = parser.parse_args()
    modules = {}
    for name in ("sqlite3", "json", "hashlib", "argparse", "multiprocessing"):
        module = importlib.import_module(name)
        modules[name] = getattr(module, "__version__", "available")
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "wal-check.db"
        connection = sqlite3.connect(path)
        mode = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        connection.execute("CREATE TABLE environment_check(value TEXT)")
        connection.commit()
        connection.close()
    result = {
        "ok": sys.version_info >= (3, 11) and str(mode).lower() == "wal",
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "sqlite": sqlite3.sqlite_version,
        "wal_mode": str(mode).lower(),
        "modules": modules,
    }
    print(
        json.dumps(
            result,
            sort_keys=True,
            indent=None if args.compact else 2,
            separators=(",", ":") if args.compact else None,
        )
    )
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
