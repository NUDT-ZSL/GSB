"""Violation and missing-data review lists derived from the generated data."""

from __future__ import annotations

import sqlite3
from typing import Any


THRESHOLDS = {
    "residual_chlorine": {
        "low": 0.2,
        "high": None,
        "rule_name": "residual_chlorine_below_min",
    },
    "turbidity": {
        "low": None,
        "high": 1.0,
        "rule_name": "turbidity_above_max",
    },
    "ph": {
        "low": 6.5,
        "high": 8.5,
        "rule_name": "ph_outside_range",
    },
}


AUDIT_METRICS = ("residual_chlorine", "turbidity", "ph")


def _reading_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            r.reading_id,
            r.point_id,
            r.sampled_at,
            r.residual_chlorine,
            r.turbidity,
            r.ph,
            p.partition_id AS point_partition_id,
            pi.partition_id AS pipe_partition_id
        FROM readings AS r
        INNER JOIN monitoring_points AS p ON p.point_id = r.point_id
        LEFT JOIN pipes AS pi ON pi.pipe_id = p.pipe_id
        ORDER BY r.point_id, r.sampled_at
        """
    ).fetchall()


def compute_violations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for row in _reading_rows(conn):
        for metric in AUDIT_METRICS:
            value = row[metric]
            if value is None:
                continue
            thresholds = THRESHOLDS[metric]
            low = thresholds["low"]
            high = thresholds["high"]
            exceeded = (low is not None and value < low) or (
                high is not None and value > high
            )
            if not exceeded:
                continue
            violations.append(
                {
                    "point_id": row["point_id"],
                    "sampled_at": row["sampled_at"],
                    "metric": metric,
                    "value": value,
                    "rule_name": thresholds["rule_name"],
                    "threshold_low": low,
                    "threshold_high": high,
                    "point_partition_id": row["point_partition_id"],
                    "pipe_partition_id": row["pipe_partition_id"],
                }
            )
    violations.sort(
        key=lambda item: (
            item["point_id"],
            item["sampled_at"],
            item["metric"],
            item["rule_name"],
        )
    )
    return violations


def compute_manual_reviews(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = _reading_rows(conn)
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        for metric in AUDIT_METRICS:
            if row[metric] is None:
                grouped.setdefault((row["point_id"], metric), []).append(row)

    reviews: list[dict[str, Any]] = []
    for (point_id, metric), missing_rows in sorted(grouped.items()):
        interval: list[sqlite3.Row] = []
        previous_sampled_at: str | None = None
        for row in missing_rows:
            if previous_sampled_at is not None and row["sampled_at"] not in _next_slots(
                conn, point_id, previous_sampled_at
            ):
                reviews.append(_review_row(point_id, metric, interval))
                interval = []
            interval.append(row)
            previous_sampled_at = row["sampled_at"]
        if interval:
            reviews.append(_review_row(point_id, metric, interval))

    reviews.sort(
        key=lambda item: (
            item["point_id"],
            item["metric"],
            item["interval_start"],
            item["interval_end"],
        )
    )
    return reviews


def _next_slots(
    conn: sqlite3.Connection, point_id: str, sampled_at: str
) -> set[str]:
    """Return the immediate next observed sample timestamp for a point.

    Missing bursts are defined over the actual sample sequence. This avoids
    assuming a fixed cadence while still making consecutive missing samples one
    review interval.
    """

    row = conn.execute(
        """
        SELECT sampled_at
        FROM readings
        WHERE point_id = ? AND sampled_at > ?
        ORDER BY sampled_at
        LIMIT 1
        """,
        (point_id, sampled_at),
    ).fetchone()
    return {row["sampled_at"]} if row is not None else set()


def _review_row(
    point_id: str, metric: str, rows: list[sqlite3.Row]
) -> dict[str, Any]:
    first = rows[0]
    last = rows[-1]
    return {
        "point_id": point_id,
        "metric": metric,
        "interval_start": first["sampled_at"],
        "interval_end": last["sampled_at"],
        "missing_count": len(rows),
        "point_partition_id": first["point_partition_id"],
        "pipe_partition_id": first["pipe_partition_id"],
    }


def audit_signature(violations: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> str:
    from .db import canonical_json, sha256_text

    return sha256_text(canonical_json({"violations": violations, "reviews": reviews}))

