#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys


def scalar(conn: sqlite3.Connection, sql: str, *params: object) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--expected-windows", type=int, default=None)
    parser.add_argument("--expected-metric-values", type=int, default=None)
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    result = {
        "database": args.db,
        "windows": scalar(conn, "SELECT count(*) FROM windows"),
        "metric_values": scalar(conn, "SELECT count(*) FROM metric_values"),
        "remainder_ledger_rows": scalar(conn, "SELECT count(*) FROM remainder_ledger"),
        "conversion_ops": scalar(conn, "SELECT count(*) FROM conversion_ops"),
        "evaluations": scalar(conn, "SELECT count(*) FROM evaluations"),
        "duplicate_windows": scalar(
            conn,
            "SELECT count(*) FROM (SELECT scope_id,window_index,window_version "
            "FROM windows GROUP BY 1,2,3 HAVING count(*)>1)",
        ),
        "duplicate_metric_values": scalar(
            conn,
            "SELECT count(*) FROM (SELECT window_pk,metric,metric_version "
            "FROM metric_values GROUP BY 1,2,3 HAVING count(*)>1)",
        ),
        "duplicate_ledgers": scalar(
            conn,
            "SELECT count(*) FROM (SELECT window_pk,metric,metric_version "
            "FROM remainder_ledger GROUP BY 1,2,3 HAVING count(*)>1)",
        ),
        "duplicate_ops": scalar(
            conn,
            "SELECT count(*) FROM (SELECT window_pk,metric,metric_version,registration_seq "
            "FROM conversion_ops GROUP BY 1,2,3,4 HAVING count(*)>1)",
        ),
        "budget_violations": scalar(
            conn, "SELECT count(*) FROM metric_values WHERE within_budget=0"
        ),
        "staged_preps": scalar(
            conn, "SELECT count(*) FROM commit_prep WHERE status='staged'"
        ),
        "staged_threshold_replacements": scalar(
            conn, "SELECT count(*) FROM threshold_replacements WHERE status='staged'"
        ),
    }
    result["ok"] = all(
        result[name] == 0
        for name in (
            "duplicate_windows",
            "duplicate_metric_values",
            "duplicate_ledgers",
            "duplicate_ops",
            "budget_violations",
            "staged_preps",
            "staged_threshold_replacements",
        )
    )
    if args.expected_windows is not None:
        result["expected_windows_ok"] = result["windows"] == args.expected_windows
        result["ok"] &= result["expected_windows_ok"]
    if args.expected_metric_values is not None:
        result["expected_metric_values_ok"] = (
            result["metric_values"] == args.expected_metric_values
        )
        result["ok"] &= result["expected_metric_values_ok"]
    conn.close()

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
