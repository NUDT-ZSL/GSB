from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .errors import ErrorCode, EngineError, fail


SUPPORTED_TYPES = {"int", "float", "bool"}
SUPPORTED_AGGS = {"count", "sum", "mean", "var"}
SUPPORTED_FILTER_OPS = {
    "eq",
    "ne",
    "lt",
    "le",
    "gt",
    "ge",
    "in",
    "is_nan",
    "is_finite",
}


class _DuplicateKey(ValueError):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise _DuplicateKey(key)
        out[key] = value
    return out


def read_json(path: Path) -> Any:
    if not path.exists() or not path.is_file():
        raise fail(ErrorCode.FILE_NOT_FOUND, "JSON file does not exist", path=str(path))
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise fail(ErrorCode.INVALID_JSON, "JSON file is not valid UTF-8", path=str(path)) from exc
    try:
        return json.loads(text, object_pairs_hook=_pairs_hook)
    except _DuplicateKey as exc:
        raise fail(
            ErrorCode.DUPLICATE_JSON_KEY,
            "JSON object contains a duplicate key",
            path=str(path),
            key=exc.key,
        ) from exc
    except json.JSONDecodeError as exc:
        raise fail(
            ErrorCode.INVALID_JSON,
            "JSON syntax error",
            path=str(path),
            line=exc.lineno,
            column=exc.colno,
        ) from exc


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=True,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_safe_float(value: float) -> float | str:
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    return float(value)


def key_json(types: list[str], values: Iterable[Any]) -> str:
    out: dict[str, Any] = {}
    for typ, value in zip(types, values, strict=True):
        if typ == "bool":
            out[str(len(out))] = bool(value)
        elif typ == "int":
            out[str(len(out))] = int(value)
        else:
            out[str(len(out))] = json_safe_float(float(value))
    return canonical_json(out)


def key_display(columns: list[str], types: list[str], values: Iterable[Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, typ, value in zip(columns, types, values, strict=True):
        if typ == "bool":
            out[name] = bool(value)
        elif typ == "int":
            out[name] = int(value)
        else:
            out[name] = json_safe_float(float(value))
    return out


def _require_mapping(value: Any, code: str, what: str, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fail(code, f"{what} must be a JSON object", path=path)
    return value


def _require_positive_int(value: Any, code: str, what: str, **details: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise fail(code, f"{what} must be a positive integer", value=value, **details)
    return value


def _parse_digest(value: Any) -> tuple[str, str]:
    if isinstance(value, str):
        return "sha256", value.lower()
    if isinstance(value, dict):
        alg = value.get("algorithm", value.get("alg", "sha256"))
        digest = value.get("value", value.get("digest"))
        if alg != "sha256" or not isinstance(digest, str):
            raise fail(ErrorCode.BATCH_DIGEST, "unsupported or malformed content digest", digest=value)
        return alg, digest.lower()
    raise fail(ErrorCode.BATCH_DIGEST, "content digest must be a sha256 string or object")


@dataclass(frozen=True)
class Column:
    name: str
    type: str
    values: np.ndarray

    @property
    def dtype_bytes(self) -> int:
        return int(self.values.dtype.itemsize)


@dataclass(frozen=True)
class Batch:
    index: int
    row_count: int
    columns: dict[str, Column]
    digest: str
    source: str


@dataclass(frozen=True)
class AggregateSpec:
    name: str
    op: str
    column: str


@dataclass(frozen=True)
class FilterSpec:
    column: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class WindowSpec:
    length: int
    step: int
    aggregates: tuple[AggregateSpec, ...]
    partition_by: tuple[str, ...]


@dataclass(frozen=True)
class Query:
    query_id: str
    group_keys: tuple[str, ...]
    aggregates: tuple[AggregateSpec, ...]
    filters: tuple[FilterSpec, ...]
    window: WindowSpec | None
    source: str


@dataclass(frozen=True)
class AdaptiveConfig:
    row_threshold: int
    group_cardinality_threshold: int
    coarse_divisor: int


@dataclass(frozen=True)
class ExecutionConfig:
    parallel_limit: int
    per_task_memory_mib: int
    total_memory_budget_mib: int
    fixed_scale: int
    tolerance: float
    logical_clock_step: int
    shards_per_batch: int
    adaptive: AdaptiveConfig

    @property
    def per_task_memory_bytes(self) -> int:
        return self.per_task_memory_mib * 1024 * 1024

    @property
    def total_memory_budget_bytes(self) -> int:
        return self.total_memory_budget_mib * 1024 * 1024


@dataclass(frozen=True)
class Injection:
    id: str
    action: str
    trigger: dict[str, Any]
    milliseconds: int
    once: bool
    error_code: str


@dataclass(frozen=True)
class Experiment:
    root: Path
    experiment_path: Path
    batches: tuple[Batch, ...]
    query: Query
    config: ExecutionConfig
    injections: tuple[Injection, ...]
    digest: str
    query_digest: str
    config_digest: str
    injection_digest: str
    sources: tuple[str, ...]

    @property
    def columns(self) -> dict[str, str]:
        return {name: col.type for name, col in self.batches[0].columns.items()} if self.batches else {}


def _parse_column(name: str, raw: Any, expected_rows: int, batch_path: str) -> Column:
    if not isinstance(name, str) or not name:
        raise fail(ErrorCode.COLUMN_TYPE, "column name must be a non-empty string", path=batch_path)
    if not isinstance(raw, dict):
        raise fail(ErrorCode.COLUMN_TYPE, "column value must be an object", column=name, path=batch_path)
    typ = raw.get("type")
    values = raw.get("values")
    if typ not in SUPPORTED_TYPES:
        raise fail(
            ErrorCode.COLUMN_TYPE,
            "column type must be one of int, float, bool",
            column=name,
            value=typ,
            path=batch_path,
        )
    if not isinstance(values, list):
        raise fail(ErrorCode.COLUMN_LENGTH, "column values must be an array", column=name, path=batch_path)
    if len(values) != expected_rows:
        raise fail(
            ErrorCode.COLUMN_LENGTH,
            "column array length does not match row_count",
            column=name,
            expected=expected_rows,
            actual=len(values),
            path=batch_path,
        )
    try:
        if typ == "int":
            for item in values:
                if isinstance(item, bool) or not isinstance(item, int):
                    raise TypeError(f"non-integer value {item!r}")
                if item < -(2**63) or item > 2**63 - 1:
                    raise TypeError(f"integer outside int64 range: {item}")
            arr = np.asarray(values, dtype=np.int64)
        elif typ == "float":
            arr = np.asarray(values, dtype=np.float64)
            if arr.shape != (expected_rows,):
                raise TypeError("float column is not one-dimensional")
        else:
            for item in values:
                if not isinstance(item, bool):
                    raise TypeError(f"non-boolean value {item!r}")
            arr = np.asarray(values, dtype=np.bool_)
    except (TypeError, ValueError, OverflowError) as exc:
        raise fail(
            ErrorCode.COLUMN_TYPE,
            "column contains values incompatible with its declared type",
            column=name,
            type=typ,
            reason=str(exc),
            path=batch_path,
        ) from exc
    return Column(name=name, type=typ, values=arr)


def _batch_payload(index: int, row_count: int, columns: dict[str, Column]) -> dict[str, Any]:
    return {
        "batch_index": index,
        "row_count": row_count,
        "columns": {
            name: {
                "type": col.type,
                "values": col.values.tolist(),
            }
            for name, col in sorted(columns.items())
        },
    }


def _parse_batch(raw: Any, path: Path, previous_index: int | None) -> Batch:
    obj = _require_mapping(raw, ErrorCode.INVALID_JSON, "batch", str(path))
    index = obj.get("batch_index", obj.get("batch_seq"))
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise fail(ErrorCode.BATCH_SEQUENCE, "batch_index must be a non-negative integer", path=str(path))
    if previous_index is not None and index <= previous_index:
        raise fail(
            ErrorCode.BATCH_SEQUENCE,
            "batch_index values must be strictly increasing",
            previous=previous_index,
            current=index,
            path=str(path),
        )
    row_count = obj.get("row_count")
    if isinstance(row_count, bool) or not isinstance(row_count, int) or row_count < 0:
        raise fail(ErrorCode.ROW_COUNT, "row_count must be a non-negative integer", path=str(path))
    raw_columns = obj.get("columns")
    if not isinstance(raw_columns, dict):
        raise fail(ErrorCode.COLUMN_TYPE, "columns must be an object", path=str(path))
    columns = {
        name: _parse_column(name, value, row_count, str(path))
        for name, value in raw_columns.items()
    }
    if not columns:
        raise fail(ErrorCode.COLUMN_TYPE, "batch must contain at least one column", path=str(path))
    _, expected = _parse_digest(obj.get("content_digest", obj.get("digest")))
    actual = sha256_text(canonical_json(_batch_payload(index, row_count, columns)))
    if actual != expected:
        raise fail(
            ErrorCode.BATCH_DIGEST,
            "content digest does not match the column content",
            batch_index=index,
            expected=expected,
            actual=actual,
            path=str(path),
        )
    return Batch(index=index, row_count=row_count, columns=columns, digest=actual, source=str(path))


def _parse_aggregate(raw: Any, where: str) -> AggregateSpec:
    obj = _require_mapping(raw, ErrorCode.QUERY_AGGREGATE, "aggregate", where)
    name = obj.get("name")
    op = obj.get("op", obj.get("agg"))
    column = obj.get("column")
    if not isinstance(name, str) or not name:
        raise fail(ErrorCode.QUERY_AGGREGATE, "aggregate name must be a non-empty string", where=where)
    if op not in SUPPORTED_AGGS:
        raise fail(ErrorCode.QUERY_AGGREGATE, "unsupported aggregate operation", where=where, op=op)
    if not isinstance(column, str) or not column:
        raise fail(ErrorCode.QUERY_AGGREGATE, "aggregate column must be a string", where=where)
    return AggregateSpec(name=name, op=op, column=column)


def _parse_filter(raw: Any, where: str) -> FilterSpec:
    obj = _require_mapping(raw, ErrorCode.QUERY_FILTER, "filter", where)
    column = obj.get("column")
    op = obj.get("op")
    if not isinstance(column, str) or not column:
        raise fail(ErrorCode.QUERY_FILTER, "filter column must be a string", where=where)
    if op not in SUPPORTED_FILTER_OPS:
        raise fail(ErrorCode.QUERY_FILTER, "unsupported filter operation", where=where, op=op)
    return FilterSpec(column=column, op=op, value=obj.get("value"))


def _parse_query(raw: Any, path: Path) -> Query:
    obj = _require_mapping(raw, ErrorCode.INVALID_JSON, "query", str(path))
    query_id = obj.get("query_id", path.stem)
    if not isinstance(query_id, str) or not query_id:
        raise fail(ErrorCode.INVALID_JSON, "query_id must be a non-empty string", path=str(path))
    group_keys = obj.get("group_keys", [])
    if not isinstance(group_keys, list) or any(not isinstance(x, str) or not x for x in group_keys):
        raise fail(ErrorCode.QUERY_REFERENCE, "group_keys must be an array of column names", path=str(path))
    if len(set(group_keys)) != len(group_keys):
        raise fail(ErrorCode.QUERY_REFERENCE, "group_keys contains duplicates", path=str(path))
    raw_aggs = obj.get("aggregates")
    if not isinstance(raw_aggs, list) or not raw_aggs:
        raise fail(ErrorCode.QUERY_AGGREGATE, "aggregates must be a non-empty array", path=str(path))
    aggregates = tuple(_parse_aggregate(item, str(path)) for item in raw_aggs)
    names = [agg.name for agg in aggregates]
    if len(set(names)) != len(names):
        raise fail(ErrorCode.QUERY_AGGREGATE, "aggregate names must be unique", path=str(path))
    raw_filters = obj.get("filters", obj.get("filter", []))
    if isinstance(raw_filters, dict):
        raw_filters = [raw_filters]
    if not isinstance(raw_filters, list):
        raise fail(ErrorCode.QUERY_FILTER, "filters must be an object or array", path=str(path))
    filters = tuple(_parse_filter(item, str(path)) for item in raw_filters)
    window = None
    raw_window = obj.get("window")
    if raw_window is not None:
        window_obj = _require_mapping(raw_window, ErrorCode.WINDOW_ARGUMENT, "window", str(path))
        length = window_obj.get("length")
        step = window_obj.get("step")
        if isinstance(length, bool) or not isinstance(length, int) or length <= 0:
            raise fail(ErrorCode.WINDOW_ARGUMENT, "window length must be a positive integer", path=str(path))
        if isinstance(step, bool) or not isinstance(step, int) or step <= 0:
            raise fail(ErrorCode.WINDOW_ARGUMENT, "window step must be a positive integer", path=str(path))
        if step > length:
            raise fail(
                ErrorCode.WINDOW_ARGUMENT,
                "window step must not exceed window length",
                length=length,
                step=step,
                path=str(path),
            )
        raw_window_aggs = window_obj.get("aggregates", raw_aggs)
        if not isinstance(raw_window_aggs, list) or not raw_window_aggs:
            raise fail(ErrorCode.QUERY_AGGREGATE, "window aggregates must be a non-empty array", path=str(path))
        window_aggs = tuple(_parse_aggregate(item, str(path)) for item in raw_window_aggs)
        window_names = [agg.name for agg in window_aggs]
        if len(set(window_names)) != len(window_names):
            raise fail(ErrorCode.QUERY_AGGREGATE, "window aggregate names must be unique", path=str(path))
        partition_by = window_obj.get("partition_by", [])
        if not isinstance(partition_by, list) or any(
            not isinstance(x, str) or not x for x in partition_by
        ):
            raise fail(
                ErrorCode.WINDOW_ARGUMENT,
                "window partition_by must be an array of column names",
                path=str(path),
            )
        if len(set(partition_by)) != len(partition_by):
            raise fail(ErrorCode.WINDOW_ARGUMENT, "window partition_by contains duplicates", path=str(path))
        window = WindowSpec(
            length=length,
            step=step,
            aggregates=window_aggs,
            partition_by=tuple(partition_by),
        )
    return Query(
        query_id=query_id,
        group_keys=tuple(group_keys),
        aggregates=aggregates,
        filters=filters,
        window=window,
        source=str(path),
    )


def _parse_config(raw: Any, path: Path) -> ExecutionConfig:
    obj = _require_mapping(raw, ErrorCode.INVALID_JSON, "execution config", str(path))
    parallel = _require_positive_int(
        obj.get("parallel_limit"), ErrorCode.CONFIG_PARALLELISM, "parallel_limit", path=str(path)
    )
    memory = _require_positive_int(
        obj.get("per_task_memory_mib"),
        ErrorCode.CONFIG_MEMORY,
        "per_task_memory_mib",
        path=str(path),
    )
    total_memory = obj.get("total_memory_budget_mib", parallel * memory)
    total_memory = _require_positive_int(
        total_memory,
        ErrorCode.CONFIG_MEMORY,
        "total_memory_budget_mib",
        path=str(path),
    )
    if total_memory < memory:
        raise fail(
            ErrorCode.CONFIG_MEMORY,
            "total_memory_budget_mib must be at least per_task_memory_mib",
            per_task=memory,
            total=total_memory,
            path=str(path),
        )
    scale = obj.get("fixed_scale")
    if isinstance(scale, bool) or not isinstance(scale, int) or scale < 0 or scale > 18:
        raise fail(
            ErrorCode.CONFIG_FIXED_SCALE,
            "fixed_scale must be an integer in [0, 18]",
            value=scale,
            path=str(path),
        )
    tolerance = obj.get("tolerance")
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise fail(ErrorCode.CONFIG_TOLERANCE, "tolerance must be a finite non-negative number", path=str(path))
    tolerance = float(tolerance)
    if not math.isfinite(tolerance) or tolerance < 0:
        raise fail(ErrorCode.CONFIG_TOLERANCE, "tolerance must be a finite non-negative number", path=str(path))
    clock = _require_positive_int(
        obj.get("logical_clock_step"),
        ErrorCode.CONFIG_CLOCK,
        "logical_clock_step",
        path=str(path),
    )
    shards = _require_positive_int(
        obj.get("shards_per_batch"), ErrorCode.CONFIG_SHARDS, "shards_per_batch", path=str(path)
    )
    adaptive_raw = obj.get("adaptive", {})
    adaptive = _require_mapping(adaptive_raw, ErrorCode.CONFIG_ADAPTIVE, "adaptive", str(path))
    row_threshold = _require_positive_int(
        adaptive.get("row_threshold", 100_000),
        ErrorCode.CONFIG_ADAPTIVE,
        "adaptive.row_threshold",
        path=str(path),
    )
    group_threshold = _require_positive_int(
        adaptive.get("group_cardinality_threshold", 10_000),
        ErrorCode.CONFIG_ADAPTIVE,
        "adaptive.group_cardinality_threshold",
        path=str(path),
    )
    coarse_divisor = _require_positive_int(
        adaptive.get("coarse_divisor", 2),
        ErrorCode.CONFIG_ADAPTIVE,
        "adaptive.coarse_divisor",
        path=str(path),
    )
    return ExecutionConfig(
        parallel_limit=parallel,
        per_task_memory_mib=memory,
        total_memory_budget_mib=total_memory,
        fixed_scale=scale,
        tolerance=tolerance,
        logical_clock_step=clock,
        shards_per_batch=shards,
        adaptive=AdaptiveConfig(
            row_threshold=row_threshold,
            group_cardinality_threshold=group_threshold,
            coarse_divisor=coarse_divisor,
        ),
    )


def _parse_injections(raw: Any, path: Path) -> tuple[Injection, ...]:
    if raw is None:
        return ()
    obj = _require_mapping(raw, ErrorCode.INJECTION, "injection manifest", str(path))
    raw_items = obj.get("injections", [])
    if not isinstance(raw_items, list):
        raise fail(ErrorCode.INJECTION, "injections must be an array", path=str(path))
    out: list[Injection] = []
    seen: set[str] = set()
    for item in raw_items:
        entry = _require_mapping(item, ErrorCode.INJECTION, "injection", str(path))
        injection_id = entry.get("id")
        action = entry.get("action")
        trigger = entry.get("trigger", {})
        if not isinstance(injection_id, str) or not injection_id or injection_id in seen:
            raise fail(ErrorCode.INJECTION, "injection id must be unique and non-empty", path=str(path))
        seen.add(injection_id)
        if action not in {"kill", "fail", "delay"}:
            raise fail(ErrorCode.INJECTION, "unsupported injection action", id=injection_id, action=action)
        if not isinstance(trigger, dict) or not trigger:
            raise fail(ErrorCode.INJECTION, "injection trigger must be a non-empty object", id=injection_id)
        allowed_triggers = {"shard_id", "shard_ordinal", "after_committed_shards"}
        unknown = set(trigger) - allowed_triggers
        if unknown:
            raise fail(
                ErrorCode.INJECTION,
                "unsupported injection trigger key",
                id=injection_id,
                keys=sorted(unknown),
            )
        if len(trigger) != 1:
            raise fail(
                ErrorCode.INJECTION,
                "injection trigger must contain exactly one selector",
                id=injection_id,
                trigger=trigger,
            )
        if "shard_id" in trigger and not isinstance(trigger["shard_id"], str):
            raise fail(ErrorCode.INJECTION, "shard_id trigger must be a string", id=injection_id)
        for key in ("shard_ordinal", "after_committed_shards"):
            if key in trigger:
                value = trigger[key]
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise fail(
                        ErrorCode.INJECTION,
                        f"{key} trigger must be a non-negative integer",
                        id=injection_id,
                    )
        milliseconds = entry.get("milliseconds", 0)
        if isinstance(milliseconds, bool) or not isinstance(milliseconds, int) or milliseconds < 0:
            raise fail(ErrorCode.INJECTION, "milliseconds must be a non-negative integer", id=injection_id)
        if action == "delay" and milliseconds <= 0:
            raise fail(ErrorCode.INJECTION, "delay action requires positive milliseconds", id=injection_id)
        once = entry.get("once", True)
        if not isinstance(once, bool):
            raise fail(ErrorCode.INJECTION, "once must be a boolean", id=injection_id)
        if "after_committed_shards" in trigger and not once:
            raise fail(
                ErrorCode.INJECTION,
                "after_committed_shards injections must be once-only",
                id=injection_id,
            )
        if "after_committed_shards" in trigger and action == "fail":
            raise fail(
                ErrorCode.INJECTION,
                "a committed shard cannot be retroactively failed",
                id=injection_id,
            )
        error_code = entry.get("error_code", "E_INJECTED_SHARD_FAILURE")
        if not isinstance(error_code, str) or not error_code:
            raise fail(ErrorCode.INJECTION, "error_code must be a non-empty string", id=injection_id)
        out.append(
            Injection(
                id=injection_id,
                action=action,
                trigger=dict(trigger),
                milliseconds=milliseconds,
                once=once,
                error_code=error_code,
            )
        )
    return tuple(out)


def _resolve(base: Path, value: Any, code: str, what: str) -> Path:
    if not isinstance(value, str) or not value:
        raise fail(code, f"{what} path must be a non-empty string")
    path = Path(value)
    return path if path.is_absolute() else (base / path).resolve()


def _query_validate(query: Query, columns: dict[str, str], batches: tuple[Batch, ...]) -> None:
    numeric = {"int", "float", "bool"}
    for column in query.group_keys:
        if column not in columns:
            raise fail(
                ErrorCode.QUERY_REFERENCE,
                "group key references a missing column",
                column=column,
                query=query.source,
            )
    for agg in query.aggregates:
        if agg.column not in columns:
            raise fail(
                ErrorCode.QUERY_REFERENCE,
                "aggregate references a missing column",
                aggregate=agg.name,
                column=agg.column,
                query=query.source,
            )
        if agg.op != "count" and columns[agg.column] not in numeric:
            raise fail(
                ErrorCode.QUERY_TYPE,
                "aggregate requires a numeric column",
                aggregate=agg.name,
                column=agg.column,
                type=columns[agg.column],
                query=query.source,
            )
    for filt in query.filters:
        if filt.column not in columns:
            raise fail(
                ErrorCode.QUERY_REFERENCE,
                "filter references a missing column",
                column=filt.column,
                query=query.source,
            )
        typ = columns[filt.column]
        if filt.op in {"lt", "le", "gt", "ge"} and typ not in numeric:
            raise fail(
                ErrorCode.QUERY_TYPE,
                "ordered filter requires a numeric column",
                column=filt.column,
                type=typ,
                query=query.source,
            )
        if filt.op in {"is_nan", "is_finite"} and typ != "float":
            raise fail(
                ErrorCode.QUERY_TYPE,
                "NaN/finite filter requires a float column",
                column=filt.column,
                type=typ,
                query=query.source,
            )
        if filt.op == "in" and not isinstance(filt.value, list):
            raise fail(
                ErrorCode.QUERY_FILTER,
                "in filter requires an array value",
                column=filt.column,
                query=query.source,
            )
    if query.window is not None:
        for column in query.window.partition_by:
            if column not in columns:
                raise fail(
                    ErrorCode.QUERY_REFERENCE,
                    "window partition references a missing column",
                    column=column,
                    query=query.source,
                )
        for agg in query.window.aggregates:
            if agg.column not in columns:
                raise fail(
                    ErrorCode.QUERY_REFERENCE,
                    "window aggregate references a missing column",
                    aggregate=agg.name,
                    column=agg.column,
                    query=query.source,
                )
            if agg.op != "count" and columns[agg.column] not in numeric:
                raise fail(
                    ErrorCode.QUERY_TYPE,
                    "window aggregate requires a numeric column",
                    aggregate=agg.name,
                    column=agg.column,
                    type=columns[agg.column],
                    query=query.source,
                )
    for batch in batches:
        for name, typ in columns.items():
            if name not in batch.columns or batch.columns[name].type != typ:
                raise fail(
                    ErrorCode.BATCH_SCHEMA,
                    "batch schema is inconsistent",
                    batch_index=batch.index,
                    column=name,
                    path=batch.source,
                )


def load_experiment(path: str | Path) -> Experiment:
    experiment_path = Path(path).resolve()
    raw = _require_mapping(read_json(experiment_path), ErrorCode.INVALID_JSON, "experiment", str(experiment_path))
    base = experiment_path.parent
    raw_batch_files = raw.get("batch_files")
    if not isinstance(raw_batch_files, list) or not raw_batch_files:
        raise fail(ErrorCode.INVALID_JSON, "batch_files must be a non-empty array", path=str(experiment_path))
    batch_paths = [
        _resolve(base, value, ErrorCode.FILE_NOT_FOUND, "batch file") for value in raw_batch_files
    ]
    query_path = _resolve(base, raw.get("query_file"), ErrorCode.FILE_NOT_FOUND, "query file")
    config_path = _resolve(base, raw.get("config_file"), ErrorCode.FILE_NOT_FOUND, "config file")
    injections_path = None
    if raw.get("injection_file") is not None:
        injections_path = _resolve(
            base, raw.get("injection_file"), ErrorCode.FILE_NOT_FOUND, "injection manifest"
        )
    batches: list[Batch] = []
    previous: int | None = None
    for batch_path in batch_paths:
        batch = _parse_batch(read_json(batch_path), batch_path, previous)
        batches.append(batch)
        previous = batch.index
    if not batches:
        raise fail(ErrorCode.INVALID_JSON, "experiment must contain at least one batch")
    query = _parse_query(read_json(query_path), query_path)
    config = _parse_config(read_json(config_path), config_path)
    injections = (
        _parse_injections(read_json(injections_path), injections_path)
        if injections_path is not None
        else ()
    )
    columns = {name: col.type for name, col in batches[0].columns.items()}
    _query_validate(query, columns, tuple(batches))
    source_hashes: list[str] = []
    sources = [str(experiment_path)] + [str(p) for p in batch_paths] + [str(query_path), str(config_path)]
    if injections_path is not None:
        sources.append(str(injections_path))
    for source in sources:
        source_hashes.append(f"{source}:{hashlib.sha256(Path(source).read_bytes()).hexdigest()}")
    digest = sha256_text("\n".join(source_hashes))
    return Experiment(
        root=base,
        experiment_path=experiment_path,
        batches=tuple(batches),
        query=query,
        config=config,
        injections=injections,
        digest=digest,
        query_digest=sha256_text(canonical_json(read_json(query_path))),
        config_digest=sha256_text(canonical_json(read_json(config_path))),
        injection_digest=sha256_text(
            canonical_json(read_json(injections_path)) if injections_path is not None else "[]"
        ),
        sources=tuple(sources),
    )
