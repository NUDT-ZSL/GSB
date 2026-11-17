#!/usr/bin/env python3
"""Atomically inject a deterministic behavior plan into a running local backend."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lbhealth.behavior import behavior_digest, write_behavior  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, help="behavior JSON consumed by backend_sim")
    parser.add_argument("--plan-file", help="replace with the contents of this JSON file")
    parser.add_argument("--default", choices=("ok", "fail", "slow", "close"), default="ok")
    parser.add_argument("--actions", default="", help="comma-separated actions")
    parser.add_argument("--start-index", type=int, default=0)
    parser.add_argument("--slow-ms", type=int, default=200)
    args = parser.parse_args(argv)

    if args.plan_file:
        value = json.loads(Path(args.plan_file).read_text(encoding="utf-8"))
    else:
        actions = [item.strip() for item in args.actions.split(",") if item.strip()]
        value = {
            "version": 1,
            "start_index": args.start_index,
            "default": args.default,
            "actions": actions,
            "slow_ms": args.slow_ms,
        }
    normalized = write_behavior(args.path, value)
    print(
        json.dumps(
            {
                "status": "ok",
                "path": str(Path(args.path)),
                "behavior": normalized,
                "sha256": behavior_digest(normalized),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
