from __future__ import annotations

from typing import Any


def matched_row(
    left_row: dict[str, Any],
    right_row: dict[str, Any],
    partition_id: int,
) -> dict[str, Any]:
    return {
        "left_row_id": int(left_row["row_id"]),
        "right_row_id": int(right_row["row_id"]),
        "left": left_row["values"],
        "right": right_row["values"],
        "source_partitions": {"left": partition_id, "right": partition_id},
    }


def left_only_row(
    left_row: dict[str, Any],
    left_partition: int,
) -> dict[str, Any]:
    return {
        "left_row_id": int(left_row["row_id"]),
        "right_row_id": None,
        "left": left_row["values"],
        "right": None,
        "source_partitions": {"left": left_partition, "right": None},
    }


def right_only_row(
    right_row: dict[str, Any],
    right_partition: int,
) -> dict[str, Any]:
    return {
        "left_row_id": None,
        "right_row_id": int(right_row["row_id"]),
        "left": None,
        "right": right_row["values"],
        "source_partitions": {"left": None, "right": right_partition},
    }

