#!/usr/bin/env python3
"""Re-read generated artifacts and print the reproducible validation ledger."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from weld_train.io_utils import atomic_write_json
from weld_train.validation import recompute_validation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent.parent / "data",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="also write validation/recomputed.json",
    )
    args = parser.parse_args()
    record = recompute_validation(args.data_dir)
    if args.write:
        atomic_write_json(
            args.data_dir / "validation" / "recomputed.json", record
        )
    print(
        json.dumps(
            {
                "ok": record["pass"],
                "seed": record["seed"],
                "segment_results": [
                    {
                        "segment_id": item["segment_id"],
                        "pass": item["pass"],
                        "snapshot_version": item["snapshot_version"],
                        "geometry": item["checks"]["geometry_distance"],
                        "boundary": item["checks"]["boundary"],
                    }
                    for item in record["segments"]
                ],
                "realtime_replay_consistent": all(
                    item["pass"] for item in record["consistency"]
                ),
                "stored_record_comparison": record.get(
                    "stored_record_comparison"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if record.get("pass") else 1


if __name__ == "__main__":
    raise SystemExit(main())
