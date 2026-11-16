"""Helper process that intentionally dies inside a persisted replay batch."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .service import LabSession


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--rate", type=int, default=10)
    parser.add_argument("--concurrency", type=int, default=2)
    parser.add_argument("--crash-batch", type=int, default=1)
    args = parser.parse_args(argv)

    with LabSession.open(args.root) as session:
        def crash_hook(batch: object) -> None:
            batch_id = getattr(batch, "batch_id")
            if batch_id == args.crash_batch:
                print(
                    f"intentional forced termination inside batch {batch_id}",
                    file=sys.stderr,
                    flush=True,
                )
                # os._exit skips finally blocks and destructors, exactly like a
                # forced process termination.  Only committed SQLite state remains.
                os._exit(99)

        session.replay(
            replay_rate=args.rate,
            concurrency=args.concurrency,
            reset=True,
            crash_hook=crash_hook,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

