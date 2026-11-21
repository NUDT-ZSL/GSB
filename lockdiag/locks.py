"""Resource conflict rules, lock holders and FIFO queue primitives."""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

from .util import canonical_json


def _range_contains(row: int | None, start: int | None, end: int | None) -> bool:
    if row is None or start is None or end is None:
        return False
    return start <= row <= end


def resources_conflict(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return whether two requests on possibly different resource shapes conflict."""

    if a["table_name"] != b["table_name"]:
        return False
    a_type = a["resource_type"]
    b_type = b["resource_type"]
    a_mode = a["mode"]
    b_mode = b["mode"]

    if a_type == "table" or b_type == "table":
        if a_type == "table" and b_type == "table":
            return a_mode == "exclusive" or b_mode == "exclusive"
        if a_type == "table":
            return a_mode == "exclusive" or b_mode == "exclusive"
        return b_mode == "exclusive" or a_mode == "exclusive"

    if a_mode == "shared" and b_mode == "shared":
        return False

    if a_type == "row" and b_type == "row":
        return a.get("row_id") == b.get("row_id")
    if a_type == "range" and b_type == "range":
        return not (
            int(a["range_end"]) < int(b["range_start"])
            or int(b["range_end"]) < int(a["range_start"])
        )
    if a_type == "row" and b_type == "range":
        return _range_contains(a.get("row_id"), b.get("range_start"), b.get("range_end"))
    if a_type == "range" and b_type == "row":
        return _range_contains(b.get("row_id"), a.get("range_start"), a.get("range_end"))
    return False


def conflicting_holders(
    connection: sqlite3.Connection, request: dict[str, Any], exclude_tx: str | None = None
) -> list[dict[str, Any]]:
    holders = [
        dict(row)
        for row in connection.execute(
            """
            SELECT holder_id, resource_key, resource_type, table_name, row_id,
                   range_start, range_end, mode, tx_id, op_seq, acquired_sample
            FROM lock_holders
            WHERE table_name=?
            ORDER BY acquired_sample, tx_id, resource_key
            """,
            (request["table_name"],),
        )
    ]
    return [
        holder
        for holder in holders
        if holder["tx_id"] != exclude_tx and resources_conflict(holder, request)
    ]


def queued_entries(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in connection.execute(
            "SELECT * FROM wait_queue WHERE status='QUEUED' ORDER BY queue_no"
        )
    ]


def queue_snapshot(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "queue_no": row["queue_no"],
            "wait_id": row["wait_id"],
            "tx_id": row["tx_id"],
            "resource_key": row["resource_key"],
            "requested_mode": row["requested_mode"],
            "status": row["status"],
        }
        for row in connection.execute(
            "SELECT * FROM wait_queue ORDER BY queue_no"
        )
    ]


def lock_snapshot(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        {
            "resource_key": row["resource_key"],
            "resource_type": row["resource_type"],
            "table_name": row["table_name"],
            "row_id": row["row_id"],
            "range_start": row["range_start"],
            "range_end": row["range_end"],
            "mode": row["mode"],
            "tx_id": row["tx_id"],
            "acquired_sample": row["acquired_sample"],
        }
        for row in connection.execute(
            "SELECT * FROM lock_holders ORDER BY table_name, resource_key, tx_id"
        )
    ]


def validate_holder_set(holders: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return deterministic pairwise conflicts among granted holders."""

    values = list(holders)
    conflicts: list[dict[str, Any]] = []
    for index, left in enumerate(values):
        for right in values[index + 1 :]:
            if left["tx_id"] == right["tx_id"]:
                continue
            if resources_conflict(left, right):
                conflicts.append(
                    {
                        "tx_a": left["tx_id"],
                        "tx_b": right["tx_id"],
                        "resource_a": left["resource_key"],
                        "resource_b": right["resource_key"],
                    }
                )
    return conflicts


def holder_request(
    resource_key: str,
    resource_type: str,
    table_name: str,
    mode: str,
    row_id: int | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
) -> dict[str, Any]:
    return {
        "resource_key": resource_key,
        "resource_type": resource_type,
        "table_name": table_name,
        "row_id": row_id,
        "range_start": range_start,
        "range_end": range_end,
        "mode": mode,
    }


def resource_from_queue_row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    import json

    key = json.loads(row["resource_key"])
    kind = key[0]
    if kind == "row":
        return holder_request(
            row["resource_key"], "row", key[1], row["requested_mode"], row_id=int(key[2])
        )
    if kind == "range":
        return holder_request(
            row["resource_key"],
            "range",
            key[1],
            row["requested_mode"],
            range_start=int(key[3]),
            range_end=int(key[4]),
        )
    return holder_request(row["resource_key"], "table", key[1], row["requested_mode"])


def request_from_operation(operation: dict[str, Any]) -> dict[str, Any]:
    return holder_request(
        operation["resource_key"],
        operation["resource_type"],
        operation["table_name"],
        operation["requested_mode"],
        row_id=operation.get("row_id"),
        range_start=operation.get("range_start"),
        range_end=operation.get("range_end"),
    )


def holder_tx_list(connection: sqlite3.Connection, request: dict[str, Any], exclude_tx: str) -> list[str]:
    holders = conflicting_holders(connection, request, exclude_tx)
    return sorted({holder["tx_id"] for holder in holders})


def primary_blocker(
    connection: sqlite3.Connection, request: dict[str, Any], exclude_tx: str
) -> tuple[str | None, list[str]]:
    holder_ids = holder_tx_list(connection, request, exclude_tx)
    if holder_ids:
        return holder_ids[0], holder_ids
    for queued in queued_entries(connection):
        if queued["tx_id"] == exclude_tx:
            continue
        queued_request = resource_from_queue_row(queued)
        if resources_conflict(queued_request, request):
            return queued["tx_id"], [queued["tx_id"]]
    return None, []
