"""Explicit NULL-aware joins over already-pushed-down partition rows."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .reference import POINT_ROW_COLUMNS, PIPE_ROW_COLUMNS, row_sort_key, sort_rows


NO_PIPE_PARTITION = "__no_pipe__"
NO_POINT_PARTITION = "__no_point__"


@dataclass(frozen=True)
class JoinTaskResult:
    join_task_key: str
    pipe_partition_id: str
    point_partition_id: str
    rows: list[dict[str, Any]]


def _blank_join_row() -> dict[str, Any]:
    row = {column: None for column in POINT_ROW_COLUMNS + PIPE_ROW_COLUMNS}
    row["join_null_source"] = None
    return row


def make_matched_row(
    point_row: dict[str, Any], pipe_row: dict[str, Any]
) -> dict[str, Any]:
    row = _blank_join_row()
    for column in POINT_ROW_COLUMNS:
        row[column] = point_row.get(column)
    for column in PIPE_ROW_COLUMNS:
        row[column] = pipe_row.get(column)
    row["join_null_source"] = "none"
    return row


def make_point_only_row(point_row: dict[str, Any]) -> dict[str, Any]:
    row = _blank_join_row()
    for column in POINT_ROW_COLUMNS:
        row[column] = point_row.get(column)
    row["join_null_source"] = (
        "point_key_null"
        if point_row.get("point_pipe_id") is None
        else "missing_pipe_reference"
    )
    return row


def make_pipe_only_row(pipe_row: dict[str, Any]) -> dict[str, Any]:
    row = _blank_join_row()
    for column in PIPE_ROW_COLUMNS:
        row[column] = pipe_row.get(column)
    row["join_null_source"] = (
        "pipe_key_null"
        if pipe_row.get("pipe_id") is None
        else "missing_point_reference"
    )
    return row


def join_task_key(
    pipe_partition_id: str | None, point_partition_id: str | None
) -> str:
    pipe_key = pipe_partition_id or NO_PIPE_PARTITION
    point_key = point_partition_id or NO_POINT_PARTITION
    return f"{pipe_key}|{point_key}"


def pipe_index(
    pipe_rows: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pipe_rows:
        pipe_id = row.get("pipe_id")
        if pipe_id is not None:
            index[pipe_id].append(row)
    return dict(index)


def execute_nested_loop_join(
    point_rows: Iterable[dict[str, Any]],
    pipe_rows: Iterable[dict[str, Any]],
    join_type: str,
    *,
    point_partition_filter: set[str] | None = None,
    pipe_partition_filter: set[str] | None = None,
    include_unmatched_points: bool = True,
    include_unmatched_pipes: bool = True,
) -> dict[str, JoinTaskResult]:
    """Join and group rows by the partition pair that produced them.

    The optional partition filters let an incremental run recompute only the
    affected join tasks while still deriving matches from the complete opposite
    side.
    """

    if join_type not in {"inner", "left", "right", "full"}:
        raise ValueError(f"unsupported join type: {join_type}")
    point_list = list(point_rows)
    pipe_list = list(pipe_rows)
    by_pipe = pipe_index(pipe_list)
    tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)
    # Compute global matches first. When an incremental call filters the point
    # partitions being joined, unmatched-pipe detection must still consider
    # points from partitions that were reused rather than recomputed.
    matched_pipe_row_ids: set[int] = set()
    for point in point_list:
        point_pipe_id = point.get("point_pipe_id")
        if point_pipe_id is None:
            continue
        for pipe in by_pipe.get(point_pipe_id, []):
            if pipe.get("pipe_row_id") is not None:
                matched_pipe_row_ids.add(pipe["pipe_row_id"])

    for point in point_list:
        point_partition = point.get("point_partition_id")
        if (
            point_partition_filter is not None
            and point_partition not in point_partition_filter
        ):
            continue
        point_pipe_id = point.get("point_pipe_id")
        matches = by_pipe.get(point_pipe_id, []) if point_pipe_id is not None else []
        if matches:
            for pipe in matches:
                pipe_partition = pipe.get("pipe_partition_id")
                key = join_task_key(pipe_partition, point_partition)
                tasks[key].append(make_matched_row(point, pipe))
        elif join_type in {"left", "full"} and include_unmatched_points:
            key = join_task_key(None, point_partition)
            tasks[key].append(make_point_only_row(point))

    if join_type in {"right", "full"} and include_unmatched_pipes:
        for pipe in pipe_list:
            pipe_partition = pipe.get("pipe_partition_id")
            if (
                pipe_partition_filter is not None
                and pipe_partition not in pipe_partition_filter
            ):
                continue
            if pipe.get("pipe_row_id") in matched_pipe_row_ids:
                continue
            key = join_task_key(pipe_partition, None)
            tasks[key].append(make_pipe_only_row(pipe))

    results: dict[str, JoinTaskResult] = {}
    for key, rows in tasks.items():
        sorted_rows = sort_rows(rows)
        first = sorted_rows[0]
        results[key] = JoinTaskResult(
            join_task_key=key,
            pipe_partition_id=first.get("pipe_partition_id") or NO_PIPE_PARTITION,
            point_partition_id=first.get("point_partition_id") or NO_POINT_PARTITION,
            rows=sorted_rows,
        )
    return results


def execute_selective_join(
    point_rows: Iterable[dict[str, Any]],
    pipe_rows: Iterable[dict[str, Any]],
    join_type: str,
    *,
    recompute_point_partitions: set[str],
    recompute_pipe_partitions: set[str],
) -> dict[str, JoinTaskResult]:
    """Recompute only join outputs touched by changed partition inputs.

    Rows in unaffected tasks are reused by the caller. This function still
    derives each affected task from the complete opposite side, so a changed
    point still finds its pipe in an unaffected partition and vice versa.
    """

    if join_type not in {"inner", "left", "right", "full"}:
        raise ValueError(f"unsupported join type: {join_type}")
    point_list = list(point_rows)
    pipe_list = list(pipe_rows)
    by_pipe = pipe_index(pipe_list)
    points_by_pipe: dict[str, list[dict[str, Any]]] = defaultdict(list)
    matched_pipe_row_ids: set[int] = set()
    for point in point_list:
        point_pipe_id = point.get("point_pipe_id")
        if point_pipe_id is None:
            continue
        points_by_pipe[point_pipe_id].append(point)
        for pipe in by_pipe.get(point_pipe_id, []):
            if pipe.get("pipe_row_id") is not None:
                matched_pipe_row_ids.add(pipe["pipe_row_id"])

    tasks: dict[str, list[dict[str, Any]]] = defaultdict(list)

    # Point-driven side: all matching pipes are considered, regardless of the
    # pipe partition being recomputed.
    for point in point_list:
        point_partition = point.get("point_partition_id")
        if point_partition not in recompute_point_partitions:
            continue
        point_pipe_id = point.get("point_pipe_id")
        matches = by_pipe.get(point_pipe_id, []) if point_pipe_id is not None else []
        if matches:
            for pipe in matches:
                pipe_partition = pipe.get("pipe_partition_id")
                key = join_task_key(pipe_partition, point_partition)
                tasks[key].append(make_matched_row(point, pipe))
        elif join_type in {"left", "full"}:
            key = join_task_key(None, point_partition)
            tasks[key].append(make_point_only_row(point))

    # Pipe-driven side: only points from partitions not already handled above.
    for pipe in pipe_list:
        pipe_partition = pipe.get("pipe_partition_id")
        if pipe_partition not in recompute_pipe_partitions:
            continue
        matching_points = (
            points_by_pipe.get(pipe.get("pipe_id"), [])
            if pipe.get("pipe_id") is not None
            else []
        )
        for point in matching_points:
            point_partition = point.get("point_partition_id")
            if point_partition in recompute_point_partitions:
                continue
            key = join_task_key(pipe_partition, point_partition)
            tasks[key].append(make_matched_row(point, pipe))
        if (
            join_type in {"right", "full"}
            and pipe.get("pipe_row_id") not in matched_pipe_row_ids
        ):
            key = join_task_key(pipe_partition, None)
            tasks[key].append(make_pipe_only_row(pipe))

    results: dict[str, JoinTaskResult] = {}
    for key, rows in tasks.items():
        sorted_rows = sort_rows(rows)
        first = sorted_rows[0]
        results[key] = JoinTaskResult(
            join_task_key=key,
            pipe_partition_id=first.get("pipe_partition_id") or NO_PIPE_PARTITION,
            point_partition_id=first.get("point_partition_id") or NO_POINT_PARTITION,
            rows=sorted_rows,
        )
    return results


def all_join_task_keys(
    point_partition_ids: Iterable[str],
    pipe_partition_ids: Iterable[str],
    join_type: str,
) -> list[tuple[str, str, str]]:
    """Describe the full join task graph (including empty potential tasks)."""

    points = sorted(set(point_partition_ids))
    pipes = sorted(set(pipe_partition_ids))
    tasks: list[tuple[str, str, str]] = []
    for point_partition in points:
        for pipe_partition in pipes:
            tasks.append(
                (
                    join_task_key(pipe_partition, point_partition),
                    pipe_partition,
                    point_partition,
                )
            )
    if join_type in {"left", "full"}:
        for point_partition in points:
            tasks.append(
                (
                    join_task_key(None, point_partition),
                    NO_PIPE_PARTITION,
                    point_partition,
                )
            )
    if join_type in {"right", "full"}:
        for pipe_partition in pipes:
            tasks.append(
                (
                    join_task_key(pipe_partition, None),
                    pipe_partition,
                    NO_POINT_PARTITION,
                )
            )
    return sorted(tasks)


def flatten_task_results(
    tasks: Iterable[JoinTaskResult],
) -> list[dict[str, Any]]:
    rows = [row for task in tasks for row in task.rows]
    return sorted(rows, key=row_sort_key)

