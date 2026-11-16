from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import httpx

from .config import TaskConfig
from .db import Database
from .scheduler import CrashInterrupt, run_task

FORCED_EXIT_CODE = 73


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Catalog pipeline worker")
    parser.add_argument("--config", required=True, help="JSON config with task + site_base_url")
    parser.add_argument("--db", required=True, help="SQLite database path")
    args = parser.parse_args(argv)

    with open(args.config, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = TaskConfig.from_dict(raw["task"])
    base_url = raw["site_base_url"]
    crash_after = int(os.environ.get("CRASH_AFTER_CONFIRMED", "0")) or None

    db = Database(args.db)

    async def _run() -> dict:
        async with httpx.AsyncClient(base_url=base_url) as client:
            return await run_task(cfg, db, client, crash_after_confirmed=crash_after)

    try:
        summary = asyncio.run(_run())
    except CrashInterrupt:
        # Hard kill: checkpoint is already persisted in SQLite; no exit
        # report is written on purpose.
        db.close()
        os._exit(FORCED_EXIT_CODE)
    db.close()
    json.dump(summary, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
