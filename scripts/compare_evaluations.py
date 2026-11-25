#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def fetch_rows(db_path: str, threshold_version: str | None) -> list[tuple]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    if threshold_version is None:
        row = conn.execute(
            "SELECT threshold_version FROM threshold_current WHERE id=1"
        ).fetchone()
        if row is None:
            raise SystemExit(f"{db_path} has no current threshold version")
        threshold_version = row["threshold_version"]
    rows = conn.execute(
        """
        SELECT w.scope_id, w.window_index, e.metric, e.metric_version,
               e.verdict, e.safe_verdict, e.precision_sensitive,
               e.higher_scale_flip
        FROM evaluations e
        JOIN windows w ON w.window_pk = e.window_pk
        WHERE e.threshold_version=?
        ORDER BY w.scope_id, w.window_index, e.metric, e.metric_version
        """,
        (threshold_version,),
    ).fetchall()
    conn.close()
    return [tuple(row) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--incremental-db", required=True)
    parser.add_argument("--truth-db", required=True)
    parser.add_argument("--threshold-version", default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    incremental = fetch_rows(args.incremental_db, args.threshold_version)
    truth = fetch_rows(args.truth_db, args.threshold_version)
    identical = incremental == truth
    result = {
        "ok": identical,
        "incremental_rows": len(incremental),
        "truth_rows": len(truth),
        "identical": identical,
    }
    if not identical:
        for index, (left, right) in enumerate(zip(incremental, truth)):
            if left != right:
                result["first_diff"] = {"index": index, "incremental": left, "truth": right}
                break
        if "first_diff" not in result:
            result["first_diff"] = {"length_mismatch": True}
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if identical else 1


if __name__ == "__main__":
    sys.exit(main())
