#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def atomic_replace_check(root: Path) -> dict[str, object]:
    target = root / "atomic-target.bin"
    temp = root / ".atomic-temp.tmp"
    target.write_bytes(b"old")
    temp.write_bytes(b"new")
    with temp.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(temp, target)
    return {
        "ok": target.read_bytes() == b"new" and not temp.exists(),
        "primitive": "os.replace (same-directory rename)",
    }


def sqlite_lock_check(root: Path) -> dict[str, object]:
    db_path = root / "lock-check.db"
    first = sqlite3.connect(db_path, timeout=1.0, isolation_level=None)
    first.execute("PRAGMA journal_mode=WAL")
    first.execute("CREATE TABLE probe(value INTEGER)")
    first.execute("BEGIN IMMEDIATE")
    script = (
        "import sqlite3,sys,time\n"
        f"c=sqlite3.connect({str(db_path)!r},timeout=0.05,isolation_level=None)\n"
        "try:\n"
        " c.execute('BEGIN IMMEDIATE')\n"
        "except sqlite3.OperationalError as e:\n"
        " print('locked' if 'locked' in str(e).lower() else str(e))\n"
        " sys.exit(0)\n"
        "print('unexpected-lock')\n"
        "sys.exit(1)\n"
    )
    blocked = subprocess.run(
        [sys.executable, "-c", script],
        text=True,
        capture_output=True,
        check=False,
    )
    first.execute("COMMIT")
    first.close()
    return {
        "blocked_while_held": blocked.returncode == 0
        and blocked.stdout.strip() == "locked",
        "primitive": "SQLite BEGIN IMMEDIATE + WAL",
    }


def advisory_file_lock_check(root: Path) -> dict[str, object]:
    from flowagg.locking import FileLock

    lock_path = root / "advisory.lock"
    script = (
        "from flowagg.locking import FileLock\n"
        "from flowagg.errors import FlowAggError\n"
        "try:\n"
        f" with FileLock({str(lock_path)!r}, timeout_seconds=0.05):\n"
        "  print('unexpected-lock')\n"
        "except FlowAggError as e:\n"
        " print(e.code)\n"
    )
    with FileLock(lock_path, timeout_seconds=1.0):
        blocked = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            text=True,
            capture_output=True,
            check=False,
        )
    return {
        "blocked_while_held": blocked.stdout.strip() == "E_LOCK_TIMEOUT",
        "primitive": "msvcrt.locking on Windows, fcntl.flock elsewhere",
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        checks = {
            "advisory_file_lock": advisory_file_lock_check(root),
            "atomic_replace": atomic_replace_check(root),
            "platform": sys.platform,
            "python": sys.version.split()[0],
            "sqlite": sqlite3.sqlite_version,
            "sqlite_lock": sqlite_lock_check(root),
            "windows_runtime": os.name == "nt",
        }
    checks["ok"] = all(
        checks[name]["ok"] if name == "atomic_replace" else checks[name]["blocked_while_held"]
        for name in ("advisory_file_lock", "atomic_replace", "sqlite_lock")
    )
    print(json.dumps(checks, sort_keys=True, indent=2))
    return 0 if checks["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

