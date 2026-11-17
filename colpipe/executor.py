"""Deterministic columnar execution with an admission-controlled thread pool."""

from __future__ import annotations

import base64
import math
import struct
import threading
import time
from collections import defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any, Iterable, Mapping

from .catalog import Dataset, PipelineRequest, Table
from .codes import E_BUDGET, E_DIVERGE, E_PLAN
from .errors import PipeError
from .plan import PlanBundle, walk_plan
from .query import BoundQuery, BoundRef, Predicate
from .util import canonical_json, json_value, sha256_bytes, sha256_json


@dataclass
class ChunkColumn:
    name: str
    type: str
    values: list[Any]
    null_bitmap: list[bool]

    @property
    def row_count(self) -> int:
        return len(self.values)


@dataclass
class Chunk:
    alias: str
    columns: list[ChunkColumn]

    @property
    def row_count(self) -> int:
        return self.columns[0].row_count if self.columns else 0

    @property
    def column_map(self) -> dict[str, ChunkColumn]:
        return {column.name: column for column in self.columns}

    def value(self, column: str, row: int) -> Any:
        return self.column_map[column].values[row]


@dataclass
class TaskSpec:
    task_id: str
    scan_alias: str
    table_name: str
    part: int
    input_start: int
    input_end: int
    columns: list[dict[str, Any]]
    filters: list[dict[str, Any]]
    peak_memory_bytes: int

    @property
    def input_rows(self) -> int:
        return max(0, self.input_end - self.input_start)

    def public_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "scan_alias": self.scan_alias,
            "table": self.table_name,
            "part": self.part,
            "input_range": [self.input_start, self.input_end],
            "input_rows": self.input_rows,
            "columns": [column["ref"] for column in self.columns],
            "pushed_filter_count": len(self.filters),
            "peak_memory_bytes": self.peak_memory_bytes,
        }


@dataclass
class TaskRun:
    spec: TaskSpec
    chunk: Chunk
    trace: dict[str, Any]
    predicate_evaluations: int


@dataclass
class ExecutionResult:
    mode: str
    columns: list[dict[str, Any]]
    rows: list[list[Any]]
    group_keys: list[dict[str, Any]]
    canonical_bytes: bytes
    canonical_sha256: str
    trace: list[dict[str, Any]]
    stats: dict[str, Any]
    plan_summary_sha256: str

    def to_artifact(self, *, embed_bytes: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "mode": self.mode,
            "plan_summary_sha256": self.plan_summary_sha256,
            "result": {
                "columns": self.columns,
                "rows": self.rows,
                "group_keys": self.group_keys,
                "canonical_sha256": self.canonical_sha256,
                "canonical_byte_length": len(self.canonical_bytes),
            },
            "trace": self.trace,
            "stats": self.stats,
        }
        if embed_bytes:
            payload["result"]["canonical_bytes_base64"] = base64.b64encode(self.canonical_bytes).decode("ascii")
        return payload


def _predicate_from_dict(value: Mapping[str, Any]) -> Predicate:
    alias, column = str(value["column"]).split(".", 1)
    return Predicate(BoundRef(alias, column, str(value["type"])), str(value["op"]), tuple(value.get("values", [])))


def _column_width(table: Table, column_name: str) -> int:
    column = table.column(column_name)
    if column.type == "bool":
        return 1
    if column.type in {"int64", "float64"}:
        return 8
    maximum = 1
    for value in column.values:
        if value is not None:
            maximum = max(maximum, len(value.encode("utf-8")))
    return 4 + maximum


def estimate_task_memory(table: Table, columns: list[dict[str, Any]], input_rows: int) -> int:
    """Conservative peak column-buffer estimate for one scan/filter task.

    Input and filtered output buffers are both accounted for, plus a row-index
    buffer and one byte of null bitmap per value.  A fixed bookkeeping overhead
    keeps empty tasks from being reported as zero bytes.
    """

    per_row = 8
    for column in columns:
        per_row += _column_width(table, column["column"]) + 1
    return 4096 + 2 * max(0, input_rows) * per_row


def _task_columns_for_scan(node: dict[str, Any]) -> list[dict[str, Any]]:
    return list(node["attrs"]["columns"])


def _find_scans(plan: dict[str, Any]) -> dict[str, tuple[dict[str, Any], list[dict[str, Any]]]]:
    scans: dict[str, tuple[dict[str, Any], list[dict[str, Any]]]] = {}
    parent: dict[str, dict[str, Any]] = {}

    def visit(node: dict[str, Any], parent_node: dict[str, Any] | None = None) -> None:
        if parent_node is not None:
            parent[node["id"]] = parent_node
        if node["op"] == "Scan":
            filters: list[dict[str, Any]] = []
            scan_parent = parent.get(node["id"])
            if scan_parent is not None and scan_parent["op"] == "Filter":
                filters = list(scan_parent["attrs"].get("conditions", []))
            scans[node["attrs"]["alias"]] = (node, filters)
        for child in node.get("children", []):
            visit(child, node)

    visit(plan)
    return scans


def build_tasks(
    bundle: PlanBundle,
    *,
    mode: str,
) -> list[TaskSpec]:
    if mode == "optimized":
        plan = bundle.optimized_plan
        split = bundle.split_plan
    elif mode == "baseline":
        plan = bundle.baseline_plan
        split = bundle.baseline_split_plan
    else:
        raise PipeError(E_PLAN, f"unknown execution mode: {mode}")
    scans = _find_scans(plan)
    split_scans = split["scans"]
    by_id: dict[str, TaskSpec] = {}
    for alias, split_scan in split_scans.items():
        scan_node, parent_filters = scans[alias]
        attrs = scan_node["attrs"]
        table = bundle.request.dataset.table(attrs["table"])
        columns = _task_columns_for_scan(scan_node)
        # Baseline execution deliberately does not fuse the logical Filter
        # above each Scan into the task: it scans every row and every column,
        # then applies the filter serially during collection.
        filters = list(attrs.get("pushed_filters", [])) if mode == "optimized" else []
        for part in split_scan["tasks"]:
            start, end = int(part["input_range"][0]), int(part["input_range"][1])
            spec = TaskSpec(
                task_id=str(part["task_id"]),
                scan_alias=alias,
                table_name=attrs["table"],
                part=int(part["part"]),
                input_start=start,
                input_end=end,
                columns=columns,
                filters=filters,
                peak_memory_bytes=estimate_task_memory(table, columns, max(0, end - start)),
            )
            by_id[spec.task_id] = spec
    return [by_id[task_id] for task_id in split["task_order"]]


def _eval_predicate(column_values: list[Any], row: int, predicate: Predicate) -> bool:
    value = column_values[row]
    op = predicate.op
    if op == "is_null":
        return value is None
    if op == "is_not_null":
        return value is not None
    if value is None:
        return False
    if op == "=":
        return value == predicate.values[0]
    if op == "!=":
        return value != predicate.values[0]
    if op == "<":
        return value < predicate.values[0]
    if op == "<=":
        return value <= predicate.values[0]
    if op == ">":
        return value > predicate.values[0]
    if op == ">=":
        return value >= predicate.values[0]
    if op == "in":
        return value in predicate.values
    if op == "not_in":
        return value not in predicate.values
    if op == "between":
        return predicate.values[0] <= value <= predicate.values[1]
    if op == "not_between":
        return value < predicate.values[0] or value > predicate.values[1]
    raise PipeError(E_PLAN, f"unsupported predicate operator in executor: {op}")


def _scan_task_worker(
    spec: TaskSpec,
    dataset: Dataset,
    submitted_ns: int,
) -> TaskRun:
    start_ns = time.perf_counter_ns()
    table = dataset.table(spec.table_name)
    predicates = [_predicate_from_dict(value) for value in spec.filters]
    source = {column["column"]: table.column(column["column"]) for column in spec.columns}
    kept: list[int] = []
    evaluations = 0
    for row in range(spec.input_start, spec.input_end):
        matched = True
        for predicate in predicates:
            evaluations += 1
            values = source[predicate.ref.column].values
            if not _eval_predicate(values, row, predicate):
                matched = False
                break
        if matched:
            kept.append(row)
    columns: list[ChunkColumn] = []
    for column_meta in spec.columns:
        column = source[column_meta["column"]]
        values = [column.values[row] for row in kept]
        nulls = [column.null_bitmap[row] for row in kept]
        columns.append(ChunkColumn(column_meta["ref"], column.type, values, nulls))
    end_ns = time.perf_counter_ns()
    trace = {
        "task_id": spec.task_id,
        "scan_alias": spec.scan_alias,
        "table": spec.table_name,
        "part": spec.part,
        "input_range": [spec.input_start, spec.input_end],
        "input_rows": spec.input_rows,
        "completed_rows": len(kept),
        "output_rows": len(kept),
        "columns": [column["ref"] for column in spec.columns],
        "pushed_filter_count": len(spec.filters),
        "queued_ms": round(max(0, start_ns - submitted_ns) / 1_000_000.0, 6),
        "start_ms": round(start_ns / 1_000_000.0, 6),
        "end_ms": round(end_ns / 1_000_000.0, 6),
        "duration_ms": round(max(0, end_ns - start_ns) / 1_000_000.0, 6),
        "peak_memory_bytes": spec.peak_memory_bytes,
    }
    return TaskRun(spec, Chunk(spec.scan_alias, columns), trace, evaluations)


def schedule_tasks(
    tasks: list[TaskSpec],
    request: PipelineRequest,
) -> list[TaskRun]:
    if not tasks:
        return []
    budget = request.settings.memory_budget_bytes
    parallelism = request.settings.parallelism
    for task in tasks:
        if task.peak_memory_bytes > budget:
            raise PipeError(
                E_BUDGET,
                "a single scan task cannot fit within the memory budget",
                "$.settings.memory_budget_bytes",
                {
                    "task_id": task.task_id,
                    "required_bytes": task.peak_memory_bytes,
                    "budget_bytes": budget,
                },
            )

    pending = list(tasks)
    schedule_start_ns = time.perf_counter_ns()
    active: dict[Future[TaskRun], tuple[TaskSpec, int]] = {}
    completed: list[TaskRun] = []
    admission_times: dict[str, int] = {}
    reserved = 0
    peak_reserved = 0
    with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="colpipe-scan") as pool:
        while pending or active:
            while pending and len(active) < parallelism:
                candidate = pending[0]
                if reserved + candidate.peak_memory_bytes > budget:
                    break
                pending.pop(0)
                reserved += candidate.peak_memory_bytes
                peak_reserved = max(peak_reserved, reserved)
                submitted_ns = time.perf_counter_ns()
                admission_times[candidate.task_id] = submitted_ns
                future = pool.submit(_scan_task_worker, candidate, request.dataset, submitted_ns)
                active[future] = (candidate, candidate.peak_memory_bytes)
            if active:
                done, _ = wait(active, return_when=FIRST_COMPLETED)
                for future in done:
                    _spec, memory = active.pop(future)
                    reserved -= memory
                    completed.append(future.result())
            elif pending:
                # This should only be reachable if the budget shrank between
                # preflight and admission; refusing is safer than bypassing it.
                candidate = pending[0]
                raise PipeError(
                    E_BUDGET,
                    "no queued task can fit within the memory budget",
                    "$.settings.memory_budget_bytes",
                    {
                        "task_id": candidate.task_id,
                        "required_bytes": candidate.peak_memory_bytes,
                        "budget_bytes": budget,
                        "reserved_bytes": reserved,
                    },
                )
    completed.sort(key=lambda run: (tasks.index(run.spec)))
    for run in completed:
        admitted_ns = admission_times.get(run.spec.task_id, schedule_start_ns)
        run.trace["budget_queue_wait_ms"] = round(
            max(0, admitted_ns - schedule_start_ns) / 1_000_000.0, 6
        )
        run.trace["admitted_monotonic_ms"] = round(admitted_ns / 1_000_000.0, 6)
        run.trace["scheduler_peak_reserved_memory_bytes"] = peak_reserved
        run.trace["scheduler_parallelism_limit"] = parallelism
        run.trace["scheduler_memory_budget_bytes"] = budget
        run.trace["scheduler_queue_note"] = "reserved_before_start; no budget bypass"
    return completed


def merge_task_chunks(alias: str, runs: list[TaskRun], columns: list[dict[str, Any]]) -> Chunk:
    merged: list[ChunkColumn] = []
    for column_meta in columns:
        values: list[Any] = []
        nulls: list[bool] = []
        for run in runs:
            output_column = run.chunk.column_map[column_meta["ref"]]
            values.extend(output_column.values)
            nulls.extend(output_column.null_bitmap)
        merged.append(ChunkColumn(column_meta["ref"], column_meta["type"], values, nulls))
    return Chunk(alias, merged)


def filter_chunk(chunk: Chunk, predicates: Iterable[Predicate]) -> tuple[Chunk, int]:
    predicates = list(predicates)
    values_by_name = {column.name: column.values for column in chunk.columns}
    kept = [
        row
        for row in range(chunk.row_count)
        if all(_eval_predicate(values_by_name[predicate.ref.text], row, predicate) for predicate in predicates)
    ]
    columns = [
        ChunkColumn(column.name, column.type, [column.values[row] for row in kept], [column.null_bitmap[row] for row in kept])
        for column in chunk.columns
    ]
    return Chunk(chunk.alias, columns), len(kept)


def _hash_key(values: list[Any]) -> tuple[Any, ...] | None:
    if any(value is None for value in values):
        return None
    return tuple(values)


def join_chunks(
    query: BoundQuery,
    left: Chunk,
    right: Chunk,
    output_columns: list[dict[str, Any]],
) -> Chunk:
    left_keys: list[BoundRef] = []
    right_keys: list[BoundRef] = []
    for pair in query.join_pairs:
        if pair.left.alias == query.from_alias:
            left_keys.append(pair.left)
            right_keys.append(pair.right)
        else:
            left_keys.append(pair.right)
            right_keys.append(pair.left)
    right_index: dict[tuple[Any, ...], list[int]] = defaultdict(list)
    right_values = {ref.text: right.column_map[ref.text].values for ref in right_keys}
    for row in range(right.row_count):
        key = _hash_key([right_values[ref.text][row] for ref in right_keys])
        if key is not None:
            right_index[key].append(row)
    left_values = {ref.text: left.column_map[ref.text].values for ref in left_keys}
    matched_pairs: list[tuple[int, int]] = []
    for left_row in range(left.row_count):
        key = _hash_key([left_values[ref.text][left_row] for ref in left_keys])
        if key is None:
            continue
        for right_row in right_index.get(key, ()):
            matched_pairs.append((left_row, right_row))
    output: list[ChunkColumn] = []
    left_map = left.column_map
    right_map = right.column_map
    for meta in output_columns:
        if meta["alias"] == query.from_alias:
            source = left_map[meta["ref"]]
            rows = [pair[0] for pair in matched_pairs]
        else:
            source = right_map[meta["ref"]]
            rows = [pair[1] for pair in matched_pairs]
        output.append(
            ChunkColumn(
                meta["ref"],
                meta["type"],
                [source.values[row] for row in rows],
                [source.null_bitmap[row] for row in rows],
            )
        )
    return Chunk("join", output)


def project_chunk(chunk: Chunk, columns: list[dict[str, Any]], alias: str = "project") -> Chunk:
    mapping = chunk.column_map
    return Chunk(
        alias,
        [
            ChunkColumn(
                meta["ref"],
                meta["type"],
                list(mapping[meta["ref"]].values),
                list(mapping[meta["ref"]].null_bitmap),
            )
            for meta in columns
        ],
    )


def _compare_scalar(left: Any, right: Any) -> int:
    if left is None and right is None:
        return 0
    if left is None:
        return -1
    if right is None:
        return 1
    if left < right:
        return -1
    if left > right:
        return 1
    return 0


def _aggregate(query: BoundQuery, chunk: Chunk) -> tuple[list[dict[str, Any]], list[list[Any]], list[dict[str, Any]]]:
    group_refs = list(query.group_by)
    group_columns = [chunk.column_map[ref.text].values for ref in group_refs]
    groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for row in range(chunk.row_count):
        values = [column[row] for column in group_columns]
        key = tuple(("null", None) if value is None else (type(value).__name__, value) for value in values)
        state = groups.get(key)
        if state is None:
            state = {
                "group_values": values,
                "aggregates": [
                    {"count": 0, "sum": 0, "value": None, "seen": False}
                    for _ in query.aggregate_items
                ],
            }
            groups[key] = state
        for index, item in enumerate(query.aggregate_items):
            aggregate_state = state["aggregates"][index]
            if item.star:
                aggregate_state["count"] += 1
                continue
            value = chunk.column_map[item.arg.text].values[row]
            if value is None:
                continue
            aggregate_state["seen"] = True
            if item.func == "COUNT":
                aggregate_state["count"] += 1
            elif item.func in {"SUM", "AVG"}:
                aggregate_state["sum"] += value
                aggregate_state["count"] += 1
            elif item.func == "MIN":
                aggregate_state["value"] = value if aggregate_state["value"] is None else min(aggregate_state["value"], value)
            elif item.func == "MAX":
                aggregate_state["value"] = value if aggregate_state["value"] is None else max(aggregate_state["value"], value)
            else:  # pragma: no cover - binder rejects unknown aggregate functions
                raise PipeError(E_PLAN, f"unknown aggregate function: {item.func}")

    if not groups and not group_refs:
        groups[()] = {
            "group_values": [],
            "aggregates": [
                {"count": 0, "sum": 0, "value": None, "seen": False}
                for _ in query.aggregate_items
            ],
        }

    records: list[dict[str, Any]] = []
    output_index = {item.alias: index for index, item in enumerate(query.select)}
    group_index = {ref.text: index for index, ref in enumerate(query.group_by)}
    symbol_by_ref = {ref.text: index for index, ref in enumerate(group_refs)}
    for state in groups.values():
        group_values = state["group_values"]
        row_values: list[Any] = []
        agg_index = 0
        for item in query.select:
            if item.kind == "group":
                row_values.append(group_values[symbol_by_ref[item.ref.text]])
            else:
                aggregate_state = state["aggregates"][agg_index]
                agg_index += 1
                if item.func == "COUNT":
                    row_values.append(aggregate_state["count"])
                elif item.func == "SUM":
                    row_values.append(aggregate_state["sum"] if aggregate_state["seen"] else None)
                elif item.func == "AVG":
                    row_values.append(
                        aggregate_state["sum"] / aggregate_state["count"]
                        if aggregate_state["seen"] and aggregate_state["count"]
                        else None
                    )
                else:
                    row_values.append(aggregate_state["value"] if aggregate_state["seen"] else None)
        records.append(
            {
                "group_values": tuple(group_values),
                "run_values": row_values,
            }
        )

    def compare_records(left: dict[str, Any], right: dict[str, Any]) -> int:
        for term in query.order_by:
            if term.target == "output":
                index = output_index[str(term.output_name)]
                left_value = left["run_values"][index]
                right_value = right["run_values"][index]
            else:
                index = int(term.group_index)
                left_value = left["group_values"][index]
                right_value = right["group_values"][index]
            result = _compare_scalar(left_value, right_value)
            if result:
                return -result if term.direction == "desc" else result
        for left_value, right_value in zip(left["group_values"], right["group_values"]):
            result = _compare_scalar(left_value, right_value)
            if result:
                return result
        return 0

    records.sort(key=cmp_to_key(compare_records))
    columns = [{"name": item.alias, "type": item.type, "nullable": item.nullable} for item in query.select]
    rows = [record["run_values"] for record in records]
    group_keys = [
        {ref.text: value for ref, value in zip(query.group_by, record["group_values"])}
        for record in records
    ]
    return columns, rows, group_keys


def serialize_result(columns: list[dict[str, Any]], rows: list[list[Any]]) -> bytes:
    buffer = bytearray(b"CPAGG1\x00")
    buffer.extend(struct.pack(">I", len(columns)))
    type_codes = {"int64": 1, "float64": 2, "bool": 3, "string": 4}
    for column in columns:
        name = str(column["name"]).encode("utf-8")
        buffer.extend(struct.pack(">I", len(name)))
        buffer.extend(name)
        buffer.extend(struct.pack(">B", type_codes[str(column["type"])]))
        buffer.extend(struct.pack(">B", 1 if column.get("nullable") else 0))
    buffer.extend(struct.pack(">Q", len(rows)))
    for row in rows:
        if len(row) != len(columns):
            raise PipeError(E_PLAN, "result row width does not match result columns")
        for value, column in zip(row, columns):
            if value is None:
                buffer.extend(b"\x00")
                continue
            buffer.extend(b"\x01")
            column_type = str(column["type"])
            if column_type == "int64":
                buffer.extend(struct.pack(">q", int(value)))
            elif column_type == "float64":
                buffer.extend(struct.pack(">d", float(value)))
            elif column_type == "bool":
                buffer.extend(struct.pack(">B", 1 if value else 0))
            elif column_type == "string":
                encoded = str(value).encode("utf-8")
                buffer.extend(struct.pack(">Q", len(encoded)))
                buffer.extend(encoded)
            else:
                raise PipeError(E_PLAN, f"cannot serialise result type: {column_type}")
    return bytes(buffer)


def execute(bundle: PlanBundle, *, mode: str) -> ExecutionResult:
    request = bundle.request
    query = request.query
    plan = bundle.optimized_plan if mode == "optimized" else bundle.baseline_plan
    plan_hash = bundle.optimized_summary_sha256 if mode == "optimized" else bundle.baseline_summary_sha256
    tasks = build_tasks(bundle, mode=mode)
    runs = schedule_tasks(tasks, request)
    if not runs:
        raise PipeError(E_PLAN, "execution produced no scan tasks")

    scans = _find_scans(plan)
    chunks: dict[str, Chunk] = {}
    table_filter_stats: dict[str, Any] = {}
    for alias in query.aliases:
        scan_node, _filters = scans[alias]
        alias_runs = [run for run in runs if run.spec.scan_alias == alias]
        chunk = merge_task_chunks(alias, alias_runs, scan_node["attrs"]["columns"])
        if mode == "optimized":
            table_filter_stats[alias] = {
                "input_rows": sum(run.spec.input_rows for run in alias_runs),
                "output_rows": chunk.row_count,
                "pushed": True,
            }
        else:
            local_predicates = [predicate for predicate in query.where if predicate.ref.alias == alias]
            before = chunk.row_count
            chunk, after = filter_chunk(chunk, local_predicates)
            table_filter_stats[alias] = {
                "input_rows": before,
                "output_rows": after,
                "pushed": False,
            }
        chunks[alias] = chunk

    left = chunks[query.from_alias]
    right = chunks[query.join_alias]
    join_node = next(node for node in walk_plan(plan) if node["op"] == "Join")
    joined = join_chunks(query, left, right, list(join_node["attrs"]["output_columns"]))
    join_output_rows = joined.row_count

    project_node = next(
        node
        for node in walk_plan(plan)
        if node["op"] == "Project" and node["id"] == "project:join-input"
    )
    joined = project_chunk(joined, list(project_node["attrs"]["columns"]), "aggregate-input")
    join_project_rows = joined.row_count
    residual = [predicate for predicate in query.where if predicate.ref.alias not in query.aliases]
    if residual:
        joined, _ = filter_chunk(joined, residual)
    aggregate_input_rows = joined.row_count

    columns, rows, group_keys = _aggregate(query, joined)
    canonical = serialize_result(columns, rows)
    result_hash = sha256_bytes(canonical)
    trace = [run.trace for run in runs]
    predicate_evaluations = sum(run.predicate_evaluations for run in runs)
    local_filter_rows = sum(stat["output_rows"] for stat in table_filter_stats.values())
    stats: dict[str, Any] = {
        "business_code": "OK",
        "mode": mode,
        "parallelism_limit": request.settings.parallelism,
        "memory_budget_bytes": request.settings.memory_budget_bytes,
        "scan_tasks": len(tasks),
        "scanned_rows": sum(task.input_rows for task in tasks),
        "scan_output_rows": sum(run.chunk.row_count for run in runs),
        "predicate_evaluations": predicate_evaluations,
        "table_filter_rows_in": sum(item["input_rows"] for item in table_filter_stats.values()),
        "table_filter_rows_out": local_filter_rows,
        "join_left_rows": left.row_count,
        "join_right_rows": right.row_count,
        "join_output_rows": join_output_rows,
        "join_project_rows": join_project_rows,
        "aggregate_input_rows": aggregate_input_rows,
        "result_rows": len(rows),
        "result_columns": [column["name"] for column in columns],
        "result_canonical_sha256": result_hash,
        "result_canonical_byte_length": len(canonical),
        "peak_task_memory_bytes": max(run.spec.peak_memory_bytes for run in runs),
        "peak_scheduler_reserved_memory_bytes": max(
            int(run.trace.get("scheduler_peak_reserved_memory_bytes", 0)) for run in runs
        ),
        "trace_sha256": sha256_json(trace),
        "task_trace_rows": sum(int(item["input_rows"]) for item in trace),
        "task_completed_rows": sum(int(item["completed_rows"]) for item in trace),
        "table_filter_stats": table_filter_stats,
    }
    # Touch every stat once to guard against accidental metadata drift before
    # snapshot/verification comparisons.
    stats["collection_stats_sha256"] = sha256_json(
        {
            "scanned_rows": stats["scanned_rows"],
            "scan_output_rows": stats["scan_output_rows"],
            "join_left_rows": stats["join_left_rows"],
            "join_right_rows": stats["join_right_rows"],
            "join_output_rows": stats["join_output_rows"],
            "aggregate_input_rows": stats["aggregate_input_rows"],
            "result_rows": stats["result_rows"],
        }
    )
    return ExecutionResult(mode, columns, rows, group_keys, canonical, result_hash, trace, stats, plan_hash)


def _result_map(result: ExecutionResult) -> dict[tuple[Any, ...], tuple[list[Any], dict[str, Any]]]:
    mapping: dict[tuple[Any, ...], tuple[list[Any], dict[str, Any]]] = {}
    for row, group_key in zip(result.rows, result.group_keys):
        key = tuple((type(value).__name__, value) for value in group_key.values())
        mapping[key] = (row, group_key)
    return mapping


def compare_results(optimized: ExecutionResult, baseline: ExecutionResult) -> dict[str, Any]:
    if optimized.canonical_bytes == baseline.canonical_bytes:
        return {
            "status": "PASS",
            "byte_equal": True,
            "optimized_sha256": optimized.canonical_sha256,
            "baseline_sha256": baseline.canonical_sha256,
            "column_order_equal": [column["name"] for column in optimized.columns]
            == [column["name"] for column in baseline.columns],
            "statistics_consistent": _statistics_consistent(optimized, baseline),
        }
    optimized_map = _result_map(optimized)
    baseline_map = _result_map(baseline)
    first_key = None
    first_field = None
    optimized_value = None
    baseline_value = None
    for key in list(optimized_map) + [key for key in baseline_map if key not in optimized_map]:
        if key not in optimized_map or key not in baseline_map:
            first_key = optimized_map.get(key, baseline_map.get(key))[1]
            first_field = "<row>"
            optimized_value = optimized_map.get(key, (None,))[0]
            baseline_value = baseline_map.get(key, (None,))[0]
            break
        opt_row, opt_group = optimized_map[key]
        base_row, _base_group = baseline_map[key]
        for index, (left, right) in enumerate(zip(opt_row, base_row)):
            if left != right or type(left) is not type(right):
                first_key = opt_group
                first_field = optimized.columns[index]["name"]
                optimized_value = left
                baseline_value = right
                break
        if first_field is not None:
            break
    if first_field is None:
        first_key = {"<columns>": True}
        first_field = "<column-order>"
    details = {
        "first_difference": {
            "group_key": json_value(first_key),
            "field": first_field,
            "optimized": json_value(optimized_value),
            "baseline": json_value(baseline_value),
        },
        "optimized_sha256": optimized.canonical_sha256,
        "baseline_sha256": baseline.canonical_sha256,
        "optimized_columns": [column["name"] for column in optimized.columns],
        "baseline_columns": [column["name"] for column in baseline.columns],
        "statistics_consistent": _statistics_consistent(optimized, baseline),
    }
    raise PipeError(E_DIVERGE, "optimized and baseline aggregate bytes differ", "$.result", details)


def _statistics_consistent(optimized: ExecutionResult, baseline: ExecutionResult) -> dict[str, Any]:
    comparable = {
        "join_left_rows": optimized.stats["join_left_rows"] == baseline.stats["join_left_rows"],
        "join_right_rows": optimized.stats["join_right_rows"] == baseline.stats["join_right_rows"],
        "join_output_rows": optimized.stats["join_output_rows"] == baseline.stats["join_output_rows"],
        "aggregate_input_rows": optimized.stats["aggregate_input_rows"] == baseline.stats["aggregate_input_rows"],
        "result_rows": optimized.stats["result_rows"] == baseline.stats["result_rows"],
        "result_columns": optimized.stats["result_columns"] == baseline.stats["result_columns"],
        "optimized_trace_scanned_rows": optimized.stats["scanned_rows"] == optimized.stats["task_trace_rows"],
        "baseline_trace_scanned_rows": baseline.stats["scanned_rows"] == baseline.stats["task_trace_rows"],
        "optimized_trace_completed_rows": optimized.stats["scan_output_rows"] == optimized.stats["task_completed_rows"],
        "baseline_trace_completed_rows": baseline.stats["scan_output_rows"] == baseline.stats["task_completed_rows"],
    }
    return {"ok": all(comparable.values()), "checks": comparable}
