#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    payload: dict[str, object] = {
        "python": sys.version.split()[0],
        "python_ok": sys.version_info >= (3, 11),
        "executable": sys.executable,
        "sqlite_version": sqlite3.sqlite_version,
    }
    try:
        import numpy

        payload["numpy"] = numpy.__version__
        payload["numpy_ok"] = True
    except Exception as error:
        payload["numpy"] = repr(error)
        payload["numpy_ok"] = False
    try:
        import pytest

        payload["pytest"] = pytest.__version__
        payload["pytest_ok"] = True
    except Exception as error:
        payload["pytest"] = repr(error)
        payload["pytest_ok"] = False
    try:
        with tempfile.NamedTemporaryFile(prefix="stream-reopt-", suffix=".db", dir=root, delete=False) as handle:
            temp_path = Path(handle.name)
        connection = sqlite3.connect(temp_path)
        connection.execute("CREATE TABLE probe(value INTEGER)")
        connection.execute("INSERT INTO probe VALUES (1)")
        connection.commit()
        value = connection.execute("SELECT value FROM probe").fetchone()[0]
        connection.close()
        os.unlink(temp_path)
        payload["workspace_writable"] = value == 1
        payload["sqlite_ok"] = True
    except Exception as error:
        payload["workspace_writable"] = False
        payload["sqlite_ok"] = False
        payload["workspace_error"] = repr(error)
    required = all(
        bool(payload.get(key))
        for key in ("python_ok", "numpy_ok", "pytest_ok", "workspace_writable", "sqlite_ok")
    )
    payload["ok"] = required
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if required else 1


if __name__ == "__main__":
    raise SystemExit(main())
