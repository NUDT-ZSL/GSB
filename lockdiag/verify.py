"""Integrity and accounting verification for a completed or paused state."""

from __future__ import annotations

import json
from typing import Any

from .clock import convert_duration, sample_by_id
from .locks import lock_snapshot, validate_holder_set
from .store import StateStore
from .util import canonical_json


def _check(name: str, ok: bool, detail: Any = None) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def verify_state(store: StateStore) -> dict[str, Any]:
    connection = store.open()
    checks: list[dict[str, Any]] = []
    integrity_ok, integrity_detail = store.integrity_check()
    checks.append(_check("sqlite_integrity", integrity_ok, integrity_detail))

    staged = connection.execute(
        "SELECT COUNT(*) AS n FROM recovery_intents WHERE status='STAGED'"
    ).fetchone()["n"]
    checks.append(_check("no_staged_recovery_intents", staged == 0, {"count": staged}))

    conflicts = validate_holder_set(lock_snapshot(connection))
    checks.append(_check("granted_locks_are_compatible", not conflicts, conflicts))

    duplicate_queue = connection.execute(
        """
        SELECT queue_no, COUNT(*) AS n
        FROM wait_queue GROUP BY queue_no HAVING COUNT(*) > 1
        """
    ).fetchall()
    checks.append(
        _check("fifo_queue_versions_unique", not duplicate_queue, [dict(row) for row in duplicate_queue])
    )

    queue_order = [
        row["queue_no"]
        for row in connection.execute("SELECT queue_no FROM wait_queue ORDER BY queue_no")
    ]
    checks.append(
        _check(
            "fifo_queue_order_stable",
            queue_order == sorted(set(queue_order)),
            {"queue_order": queue_order},
        )
    )

    duration_errors = []
    for row in connection.execute(
        "SELECT * FROM waits WHERE status='RESOLVED' ORDER BY wait_id"
    ):
        start = sample_by_id(connection, int(row["enqueue_sample"]))
        end = sample_by_id(connection, int(row["resolve_sample"]))
        conversion = convert_duration(start, end, int(row["scale"]))
        expected = (
            conversion["raw_delta"],
            conversion["rounded_units"],
            conversion["rounding_direction"],
            conversion["error_bound_micro"],
        )
        actual = (
            row["raw_delta"],
            row["rounded_units"],
            row["rounding_direction"],
            row["error_bound_micro"],
        )
        if expected != actual:
            duration_errors.append(
                {"wait_id": row["wait_id"], "expected": expected, "actual": actual}
            )
    checks.append(_check("wait_duration_recomputes", not duration_errors, duration_errors))

    account_errors = []
    for tx in connection.execute("SELECT * FROM transactions ORDER BY tx_id"):
        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(rounded_units), 0) AS units,
                   COALESCE(SUM(actual_residual_micro), 0) AS actual_error,
                   COALESCE(SUM(error_bound_micro), 0) AS error_bound
            FROM waits WHERE tx_id=? AND status='RESOLVED'
            """,
            (tx["tx_id"],),
        ).fetchone()
        expected = (
            aggregate["n"],
            aggregate["units"],
            aggregate["actual_error"],
            aggregate["error_bound"],
        )
        actual = (
            tx["wait_count"],
            tx["wait_total_units"],
            tx["wait_actual_error_micro"],
            tx["wait_error_micro"],
        )
        if expected != actual:
            account_errors.append(
                {"tx_id": tx["tx_id"], "expected": expected, "actual": actual}
            )
    checks.append(_check("transaction_wait_accounts_balance", not account_errors, account_errors))

    attribution_errors = []
    for row in connection.execute("SELECT * FROM attribution ORDER BY holder_tx, waiter_tx"):
        aggregate = connection.execute(
            """
            SELECT COUNT(*) AS n, COALESCE(SUM(rounded_units), 0) AS units,
                   COALESCE(SUM(actual_residual_micro), 0) AS actual_error,
                   COALESCE(SUM(error_bound_micro), 0) AS error_bound
            FROM waits
            WHERE tx_id=? AND COALESCE(holder_tx, 'FIFO')=? AND index_path=?
              AND status='RESOLVED'
            """,
            (row["waiter_tx"], row["holder_tx"], row["index_path"]),
        ).fetchone()
        expected = (
            aggregate["n"],
            aggregate["units"],
            aggregate["actual_error"],
            aggregate["error_bound"],
        )
        actual = (
            row["wait_count"],
            row["total_units"],
            row["actual_error_micro"],
            row["error_bound_micro"],
        )
        if expected != actual:
            attribution_errors.append(
                {
                    "holder_tx": row["holder_tx"],
                    "waiter_tx": row["waiter_tx"],
                    "index_path": row["index_path"],
                    "expected": expected,
                    "actual": actual,
                }
            )
    checks.append(
        _check("holder_waiter_index_attribution_balances", not attribution_errors, attribution_errors)
    )

    unresolved = connection.execute(
        """
        SELECT COUNT(*) AS n FROM waits w
        JOIN transactions t ON t.tx_id=w.tx_id
        WHERE w.status='WAITING' AND t.status NOT IN ('RUNNING','WAITING')
        """
    ).fetchone()["n"]
    checks.append(_check("no_orphan_wait_ledger_rows", unresolved == 0, {"count": unresolved}))

    deadlock_errors = []
    for row in connection.execute("SELECT * FROM deadlocks ORDER BY deadlock_id"):
        victim = connection.execute(
            "SELECT status FROM transactions WHERE tx_id=?", (row["victim_tx"],)
        ).fetchone()
        if victim is None or victim["status"] != "ROLLED_BACK_DEADLOCK":
            deadlock_errors.append(
                {
                    "deadlock_id": row["deadlock_id"],
                    "victim_tx": row["victim_tx"],
                    "victim_status": victim["status"] if victim else None,
                }
            )
    checks.append(_check("deadlock_victims_rolled_back", not deadlock_errors, deadlock_errors))

    clock = connection.execute("SELECT * FROM clock_state WHERE id=1").fetchone()
    sample_max = connection.execute("SELECT MAX(sample_id) AS n FROM clock_samples").fetchone()["n"]
    clock_ok = clock is not None and (
        (clock["last_sample_id"] is None and sample_max is None)
        or clock["last_sample_id"] == sample_max
    )
    checks.append(
        _check(
            "logical_clock_watermark_matches_samples",
            clock_ok,
            {"watermark": clock["last_sample_id"] if clock else None, "max_sample": sample_max},
        )
    )

    ok = all(item["ok"] for item in checks)
    return {"ok": ok, "checks": checks, "failed": [item for item in checks if not item["ok"]]}
