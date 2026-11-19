from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset local deterministic gateway state")
    parser.add_argument("--db", default="var/gateway.sqlite")
    args = parser.parse_args()
    database = Path(args.db)
    for path in (
        database,
        Path(str(database) + "-wal"),
        Path(str(database) + "-shm"),
    ):
        if path.exists():
            path.unlink()
    lock_dir = database.parent / "locks"
    if lock_dir.exists():
        shutil.rmtree(lock_dir)
    print(f"reset {database} and {lock_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
