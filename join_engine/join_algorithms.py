from __future__ import annotations

import heapq
import itertools
import os
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .canonical import (
    LogicalClock,
    filter_passes,
    key_token,
    merge_sort_key,
    result_order_key,
)
from .files import iter_jsonl
from .models import ExecutionConfig, QuerySpec
from .partitioning import partition_for_key
from .result_rows import left_only_row, matched_row, right_only_row
from .result_sink import JoinMetrics, LocalResultSink, StoredResultSink
from .selection import ALGORITHMS
from .storage import StateStore


Sink = StoredResultSink | LocalResultSink


def _partition_rows(
    state: StateStore,
    commit: dict[str, Any],
    join_key: str,
) -> Iterator[dict[str, Any]]:
    path = state.path_for(commit["relative_path"])
    for row in iter_jsonl(path):
        row["_join_key"] = row["values"].get(join_key)
        yield row


def _write_sorted_run(
    path: Path,
    rows: list[dict[str, Any]],
    *,
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> None:
    from .canonical import canonical_json

    rows.sort(key=merge_sort_key)
    size = 0
    with path.open("wb") as handle:
        for row in rows:
            encoded = (canonical_json(row) + "\n").encode("utf-8")
            handle.write(encoded)
            size += len(encoded)
    metrics.spill_bytes += size
    metrics.external_sort_runs += 1
    clock.tick(len(rows))


def external_sort_join_side(
    row_iter: Iterable[dict[str, Any]],
    *,
    chunk_rows: int,
    temp_dir: Path,
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> Iterator[dict[str, Any]]:
    temp_dir.mkdir(parents=True, exist_ok=True)
    run_paths: list[Path] = []
    buffer: list[dict[str, Any]] = []
    for row in row_iter:
        buffer.append(row)
        clock.tick(1)
        if len(buffer) >= chunk_rows:
            run_path = (
                temp_dir
                / f"run-{os.getpid()}-{uuid.uuid4().hex}-{len(run_paths):06d}.jsonl"
            )
            _write_sorted_run(run_path, buffer, clock=clock, metrics=metrics)
            run_paths.append(run_path)
            buffer = []
    if buffer:
        run_path = (
            temp_dir
            / f"run-{os.getpid()}-{uuid.uuid4().hex}-{len(run_paths):06d}.jsonl"
        )
        _write_sorted_run(run_path, buffer, clock=clock, metrics=metrics)
        run_paths.append(run_path)
    iterators = [iter_jsonl(path) for path in run_paths]
    try:
        for row in heapq.merge(*iterators, key=merge_sort_key):
            clock.tick(1)
            yield row
    finally:
        for path in run_paths:
            path.unlink(missing_ok=True)


def external_sort_result_rows(
    paths: Iterable[Path],
    *,
    chunk_rows: int,
    temp_dir: Path,
    clock: LogicalClock,
) -> Iterator[dict[str, Any]]:
    from .canonical import canonical_json

    temp_dir.mkdir(parents=True, exist_ok=True)
    run_paths: list[Path] = []
    buffer: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal buffer
        buffer.sort(key=result_order_key)
        run_path = (
            temp_dir
            / f"result-run-{os.getpid()}-{uuid.uuid4().hex}-{len(run_paths):06d}.jsonl"
        )
        with run_path.open("wb") as handle:
            for row in buffer:
                handle.write((canonical_json(row) + "\n").encode("utf-8"))
        clock.tick(len(buffer))
        run_paths.append(run_path)
        buffer = []

    for path in paths:
        for row in iter_jsonl(path):
            buffer.append(row)
            clock.tick(1)
            if len(buffer) >= chunk_rows:
                flush()
    if buffer:
        flush()
    iterators = [iter_jsonl(path) for path in run_paths]
    try:
        yield from heapq.merge(*iterators, key=result_order_key)
    finally:
        for path in run_paths:
            path.unlink(missing_ok=True)


def _hash_probe(
    *,
    build_rows: list[dict[str, Any]],
    build_rows_for_output: list[dict[str, Any]],
    build_side: str,
    probe_rows: Iterable[dict[str, Any]],
    query: QuerySpec,
    partition_id: int,
    emit: Callable[[dict[str, Any]], None],
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> None:
    index: dict[str, list[dict[str, Any]]] = {}
    for build_row in build_rows:
        key = build_row["_join_key"]
        if key is None:
            continue
        index.setdefault(key_token(key), []).append(build_row)
    metrics.rows_built += len(build_rows)
    metrics.peak_partition_rows = max(metrics.peak_partition_rows, len(build_rows))
    clock.tick(len(build_rows))
    matched_build: set[int] = set()
    probe_preserved = (query.join_type == "left" and build_side == "right") or (
        query.join_type == "right" and build_side == "left"
    )
    for probe_row in probe_rows:
        metrics.rows_probed += 1
        clock.tick(1)
        key = probe_row["_join_key"]
        candidates = index.get(key_token(key), []) if key is not None else []
        probe_matched = False
        for build_row in candidates:
            if not filter_passes(
                query.query_id,
                int(probe_row["row_id"]) if build_side == "right" else int(build_row["row_id"]),
                int(build_row["row_id"]) if build_side == "right" else int(probe_row["row_id"]),
                query.filter_selectivity,
            ):
                continue
            if build_side == "right":
                emit(matched_row(probe_row, build_row, partition_id))
            else:
                emit(matched_row(build_row, probe_row, partition_id))
            matched_build.add(int(build_row["row_id"]))
            probe_matched = True
        if probe_preserved and not probe_matched:
            if build_side == "right":
                emit(left_only_row(probe_row, partition_id))
            else:
                emit(right_only_row(probe_row, partition_id))
    build_preserved = (query.join_type == "left" and build_side == "left") or (
        query.join_type == "right" and build_side == "right"
    )
    if build_preserved:
        for build_row in build_rows_for_output:
            if int(build_row["row_id"]) in matched_build:
                continue
            if build_side == "left":
                emit(left_only_row(build_row, partition_id))
            else:
                emit(right_only_row(build_row, partition_id))


def _partition_metadata_by_id(
    commits: Iterable[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    return {int(commit["partition_id"]): commit for commit in commits}


def _selected_partitions(
    partition_count: int, partition_filter: set[int] | None
) -> list[int]:
    return sorted(partition_filter) if partition_filter is not None else list(range(partition_count))


def run_broadcast_hash(
    *,
    state: StateStore,
    sink: Sink,
    query: QuerySpec,
    execution: ExecutionConfig,
    left_commits: list[dict[str, Any]],
    right_commits: list[dict[str, Any]],
    left_stats: dict[str, Any],
    right_stats: dict[str, Any],
    partition_filter: set[int] | None,
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> None:
    if int(left_stats["estimated_total_bytes"]) <= int(right_stats["estimated_total_bytes"]):
        build_side = "left"
        build_commits = left_commits
        probe_commits = right_commits
    else:
        build_side = "right"
        build_commits = right_commits
        probe_commits = left_commits
    build_bytes = sum(int(commit["bytes"]) for commit in build_commits)
    broadcast_limit = int(execution.memory_limit_bytes * execution.broadcast_fraction)
    if build_bytes > broadcast_limit:
        raise BudgetFallback(
            f"broadcast build side bytes {build_bytes} exceed broadcast budget {broadcast_limit}"
        )
    build_rows: list[dict[str, Any]] = []
    build_rows_by_partition: dict[int, list[dict[str, Any]]] = {
        partition_id: [] for partition_id in range(execution.partitions)
    }
    join_key = query.left_key if build_side == "left" else query.right_key
    for commit in build_commits:
        partition_id = int(commit["partition_id"])
        for row in _partition_rows(state, commit, join_key):
            build_rows.append(row)
            build_rows_by_partition[partition_id].append(row)
    probe_meta = _partition_metadata_by_id(probe_commits)
    probe_key = query.right_key if build_side == "left" else query.left_key
    for partition_id in _selected_partitions(execution.partitions, partition_filter):
        existing = sink.existing(partition_id)
        if existing is not None:
            metrics.partitions_skipped += 1
            metrics.actual_algorithms[partition_id] = (
                existing.get("actual_algorithm") or metrics.requested_algorithm
            )
            continue

        def producer(emit: Callable[[dict[str, Any]], None], *, p: int = partition_id) -> None:
            probe_commit = probe_meta[p]
            _hash_probe(
                build_rows=build_rows,
                build_rows_for_output=build_rows_by_partition[p],
                build_side=build_side,
                probe_rows=_partition_rows(state, probe_commit, probe_key),
                query=query,
                partition_id=p,
                emit=emit,
                clock=clock,
                metrics=metrics,
            )

        metrics.record_actual(partition_id, "broadcast_hash")
        sink.process(partition_id, "broadcast_hash", producer)


def run_shuffled_hash(
    *,
    state: StateStore,
    sink: Sink,
    query: QuerySpec,
    execution: ExecutionConfig,
    left_commits: list[dict[str, Any]],
    right_commits: list[dict[str, Any]],
    partition_filter: set[int] | None,
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> None:
    left_meta = _partition_metadata_by_id(left_commits)
    right_meta = _partition_metadata_by_id(right_commits)
    hash_limit = int(execution.memory_limit_bytes * execution.hash_fraction)
    for partition_id in _selected_partitions(execution.partitions, partition_filter):
        left_commit = left_meta[partition_id]
        right_commit = right_meta[partition_id]
        existing = sink.existing(partition_id)
        if existing is not None:
            metrics.partitions_skipped += 1
            metrics.actual_algorithms[partition_id] = (
                existing.get("actual_algorithm") or metrics.requested_algorithm
            )
            continue
        if int(left_commit["bytes"]) <= int(right_commit["bytes"]):
            build_side = "left"
            build_commit = left_commit
            probe_commit = right_commit
            build_key = query.left_key
            probe_key = query.right_key
        else:
            build_side = "right"
            build_commit = right_commit
            probe_commit = left_commit
            build_key = query.right_key
            probe_key = query.left_key
        build_bytes = int(build_commit["bytes"])
        metrics.peak_partition_rows = max(
            metrics.peak_partition_rows,
            int(left_commit["rows"]),
            int(right_commit["rows"]),
        )
        if build_bytes > hash_limit:
            actual = "sort_merge"
            metrics.record_actual(partition_id, actual)

            def producer(emit: Callable[[dict[str, Any]], None], *, p: int = partition_id) -> None:
                _sort_merge_partition(
                    state=state,
                    left_commit=left_meta[p],
                    right_commit=right_meta[p],
                    query=query,
                    execution=execution,
                    partition_id=p,
                    emit=emit,
                    clock=clock,
                    metrics=metrics,
                )

        else:
            actual = "shuffled_hash"
            metrics.record_actual(partition_id, actual)

            def producer(emit: Callable[[dict[str, Any]], None], *, p: int = partition_id) -> None:
                build_rows = list(_partition_rows(state, build_commit, build_key))
                _hash_probe(
                    build_rows=build_rows,
                    build_rows_for_output=build_rows,
                    build_side=build_side,
                    probe_rows=_partition_rows(state, probe_commit, probe_key),
                    query=query,
                    partition_id=p,
                    emit=emit,
                    clock=clock,
                    metrics=metrics,
                )

        sink.process(partition_id, actual, producer)


def _sort_merge_partition(
    *,
    state: StateStore,
    left_commit: dict[str, Any],
    right_commit: dict[str, Any],
    query: QuerySpec,
    execution: ExecutionConfig,
    partition_id: int,
    emit: Callable[[dict[str, Any]], None],
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> None:
    temp_dir = state.root / "data" / "tmp" / f"sort-v{state.root.name}"
    left_iter = external_sort_join_side(
        _partition_rows(state, left_commit, query.left_key),
        chunk_rows=execution.chunk_rows,
        temp_dir=temp_dir / f"left-{partition_id:05d}",
        clock=clock,
        metrics=metrics,
    )
    right_iter = external_sort_join_side(
        _partition_rows(state, right_commit, query.right_key),
        chunk_rows=execution.chunk_rows,
        temp_dir=temp_dir / f"right-{partition_id:05d}",
        clock=clock,
        metrics=metrics,
    )

    def group_key(row: dict[str, Any]) -> str:
        key = row["_join_key"]
        return key_token(key)

    left_groups = itertools.groupby(left_iter, key=group_key)
    right_groups = itertools.groupby(right_iter, key=group_key)
    left_group = next(left_groups, None)
    right_group = next(right_groups, None)
    while left_group is not None or right_group is not None:
        if right_group is None or (
            left_group is not None and left_group[0] < right_group[0]
        ):
            token, rows_iter = left_group
            rows = list(rows_iter)
            if query.join_type == "left":
                for row in rows:
                    emit(left_only_row(row, partition_id))
            left_group = next(left_groups, None)
            continue
        if left_group is None or right_group[0] < left_group[0]:
            token, rows_iter = right_group
            rows = list(rows_iter)
            if query.join_type == "right":
                for row in rows:
                    emit(right_only_row(row, partition_id))
            right_group = next(right_groups, None)
            continue
        token = left_group[0]
        left_rows = list(left_group[1])
        right_rows = list(right_group[1])
        left_matched: set[int] = set()
        right_matched: set[int] = set()
        if token != "\x00null":
            for left_row in left_rows:
                for right_row in right_rows:
                    clock.tick(1)
                    if not filter_passes(
                        query.query_id,
                        int(left_row["row_id"]),
                        int(right_row["row_id"]),
                        query.filter_selectivity,
                    ):
                        continue
                    emit(matched_row(left_row, right_row, partition_id))
                    left_matched.add(int(left_row["row_id"]))
                    right_matched.add(int(right_row["row_id"]))
        if query.join_type == "left":
            for row in left_rows:
                if int(row["row_id"]) not in left_matched:
                    emit(left_only_row(row, partition_id))
        if query.join_type == "right":
            for row in right_rows:
                if int(row["row_id"]) not in right_matched:
                    emit(right_only_row(row, partition_id))
        left_group = next(left_groups, None)
        right_group = next(right_groups, None)


def run_sort_merge(
    *,
    state: StateStore,
    sink: Sink,
    query: QuerySpec,
    execution: ExecutionConfig,
    left_commits: list[dict[str, Any]],
    right_commits: list[dict[str, Any]],
    partition_filter: set[int] | None,
    clock: LogicalClock,
    metrics: JoinMetrics,
) -> None:
    left_meta = _partition_metadata_by_id(left_commits)
    right_meta = _partition_metadata_by_id(right_commits)
    for partition_id in _selected_partitions(execution.partitions, partition_filter):
        existing = sink.existing(partition_id)
        if existing is not None:
            metrics.partitions_skipped += 1
            metrics.actual_algorithms[partition_id] = (
                existing.get("actual_algorithm") or metrics.requested_algorithm
            )
            continue
        metrics.record_actual(partition_id, "sort_merge")
        metrics.peak_partition_rows = max(
            metrics.peak_partition_rows,
            int(left_meta[partition_id]["rows"]),
            int(right_meta[partition_id]["rows"]),
        )

        def producer(emit: Callable[[dict[str, Any]], None], *, p: int = partition_id) -> None:
            _sort_merge_partition(
                state=state,
                left_commit=left_meta[p],
                right_commit=right_meta[p],
                query=query,
                execution=execution,
                partition_id=p,
                emit=emit,
                clock=clock,
                metrics=metrics,
            )

        sink.process(partition_id, "sort_merge", producer)


class BudgetFallback(RuntimeError):
    pass


def execute_join(
    *,
    state: StateStore,
    sink: Sink,
    algorithm: str,
    query: QuerySpec,
    execution: ExecutionConfig,
    left_commits: list[dict[str, Any]],
    right_commits: list[dict[str, Any]],
    left_stats: dict[str, Any],
    right_stats: dict[str, Any],
    partition_filter: set[int] | None,
    clock: LogicalClock,
) -> JoinMetrics:
    if algorithm not in ALGORITHMS:
        raise ValueError(f"unsupported algorithm: {algorithm}")
    metrics = sink.metrics
    if metrics.requested_algorithm != algorithm:
        raise ValueError("sink metrics requested algorithm does not match execution algorithm")
    if algorithm == "broadcast_hash":
        try:
            run_broadcast_hash(
                state=state,
                sink=sink,
                query=query,
                execution=execution,
                left_commits=left_commits,
                right_commits=right_commits,
                left_stats=left_stats,
                right_stats=right_stats,
                partition_filter=partition_filter,
                clock=clock,
                metrics=metrics,
            )
        except BudgetFallback as exc:
            metrics.fallback_events.append(
                {
                    "partition": None,
                    "requested": "broadcast_hash",
                    "actual": "shuffled_hash",
                    "reason": str(exc),
                }
            )
            metrics.actual_algorithms.clear()
            metrics.result_rows_new = 0
            metrics.partitions_processed = 0
            metrics.partitions_skipped = 0
            metrics.rows_built = 0
            metrics.rows_probed = 0
            metrics.peak_partition_rows = 0
            run_shuffled_hash(
                state=state,
                sink=sink,
                query=query,
                execution=execution,
                left_commits=left_commits,
                right_commits=right_commits,
                partition_filter=partition_filter,
                clock=clock,
                metrics=metrics,
            )
    elif algorithm == "shuffled_hash":
        run_shuffled_hash(
            state=state,
            sink=sink,
            query=query,
            execution=execution,
            left_commits=left_commits,
            right_commits=right_commits,
            partition_filter=partition_filter,
            clock=clock,
            metrics=metrics,
        )
    else:
        run_sort_merge(
            state=state,
            sink=sink,
            query=query,
            execution=execution,
            left_commits=left_commits,
            right_commits=right_commits,
            partition_filter=partition_filter,
            clock=clock,
            metrics=metrics,
        )
    return metrics


def assemble_final_result(
    *,
    state: StateStore,
    query_id: str,
    version: int,
    result_commits: list[dict[str, Any]],
    execution: ExecutionConfig,
    clock: LogicalClock,
) -> dict[str, Any]:
    from .files import write_jsonl_rows

    final_path = state.root / "data" / f"v{version}" / "result.jsonl"
    paths = [
        state.path_for(commit["relative_path"])
        for commit in sorted(result_commits, key=lambda item: int(item["partition_id"]))
    ]
    sorted_rows = external_sort_result_rows(
        paths,
        chunk_rows=execution.chunk_rows,
        temp_dir=state.root / "data" / "tmp" / f"result-{query_id}-v{version}",
        clock=clock,
    )
    digest, size, rows = write_jsonl_rows(final_path, sorted_rows)
    return {
        "path": state.relative(final_path),
        "digest": digest,
        "rows": rows,
        "bytes": size,
    }
