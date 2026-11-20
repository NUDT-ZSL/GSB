from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any, Iterable

from .errors import (
    GSBBadBatchError,
    GSBBadConfigError,
    GSBBadQueryError,
    GSBError,
    GSBUsageError,
)
from .models import (
    AggregateSpec,
    ColumnBatch,
    EngineConfig,
    FilterSpec,
    QuerySpec,
    Sample,
    WindowSpec,
)
from .util import (
    canonical_json,
    group_key_token,
    is_finite_number,
    is_number,
    json_type_tag,
    load_json,
    normalize_digest,
    sha256_bytes,
    sha256_json,
)


CONFIG_ALIASES: dict[str, tuple[str, ...]] = {
    "time_budget_ms": (
        "time_budget_ms",
        "latency_budget_ms",
        "budget_ms",
        "query_budget_ms",
    ),
    "max_batch_rows": (
        "max_batch_rows",
        "max_rows_per_batch",
        "batch_max_rows",
        "batch_row_limit",
    ),
    "hot_key_min_rows": (
        "hot_key_min_rows",
        "hot_threshold_rows",
        "hot_key_threshold",
        "hot_row_threshold",
    ),
    "skew_ratio": ("skew_ratio", "skew_threshold", "hot_ratio", "skew_threshold_ratio"),
    "memory_limit_mib": (
        "memory_limit_mib",
        "memory_limit_mb",
        "memory_buffer_mib",
        "memory_buffer_limit_mib",
    ),
    "fixed_scale": ("fixed_scale", "decimal_scale", "fixed_point_scale", "scale"),
    "float_tolerance": ("float_tolerance", "tolerance", "float_tol"),
    "logical_clock_step_ms": (
        "logical_clock_step_ms",
        "clock_step_ms",
        "logical_clock_step",
    ),
    "exact_cost_ms_per_1k_rows": (
        "exact_cost_ms_per_1k_rows",
        "exact_ms_per_1k_rows",
        "exact_cost_ms",
    ),
    "accelerated_cost_ms_per_1k_rows": (
        "accelerated_cost_ms_per_1k_rows",
        "accelerated_ms_per_1k_rows",
        "hot_split_cost_ms_per_1k_rows",
    ),
    "switch_overhead_ms": (
        "switch_overhead_ms",
        "mode_switch_overhead_ms",
    ),
    "digest_algorithm": ("digest_algorithm", "content_digest_algorithm"),
}


def _pick(mapping: dict[str, Any], aliases: Iterable[str]) -> tuple[str | None, Any]:
    found: list[str] = []
    for key in aliases:
        if key in mapping:
            found.append(key)
    if not found:
        return None, None
    if len(set(found)) > 1:
        values = [mapping[key] for key in found]
        if any(value != values[0] for value in values[1:]):
            raise GSBBadConfigError(
                "conflicting aliases in config",
                {"aliases": found, "values": values},
            )
    return found[0], mapping[found[0]]


def _positive_number(value: Any, field: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GSBBadConfigError(f"{field} must be numeric", {"field": field})
    number = float(value)
    if not math.isfinite(number):
        raise GSBBadConfigError(f"{field} must be finite", {"field": field})
    if allow_zero:
        if number < 0:
            raise GSBBadConfigError(f"{field} must be non-negative", {"field": field})
    elif number <= 0:
        raise GSBBadConfigError(f"{field} must be positive", {"field": field})
    return number


def _positive_int(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GSBBadConfigError(f"{field} must be an integer", {"field": field})
    if allow_zero and value < 0:
        raise GSBBadConfigError(f"{field} must be non-negative", {"field": field})
    if not allow_zero and value <= 0:
        raise GSBBadConfigError(f"{field} must be positive", {"field": field})
    return value


def parse_config(raw: Any, path: Path) -> EngineConfig:
    if not isinstance(raw, dict):
        raise GSBBadConfigError("config root must be an object", {"path": str(path)})
    if isinstance(raw.get("config"), dict):
        raw = raw["config"]
    values: dict[str, Any] = {}
    for field, aliases in CONFIG_ALIASES.items():
        _, value = _pick(raw, aliases)
        values[field] = value

    required = (
        "time_budget_ms",
        "max_batch_rows",
        "hot_key_min_rows",
        "skew_ratio",
        "memory_limit_mib",
        "fixed_scale",
        "float_tolerance",
        "logical_clock_step_ms",
    )
    missing = [field for field in required if values[field] is None]
    if missing:
        raise GSBBadConfigError(
            "missing required config fields",
            {"path": str(path), "missing": missing},
        )

    budget = _positive_number(values["time_budget_ms"], "time_budget_ms")
    max_rows = _positive_int(values["max_batch_rows"], "max_batch_rows")
    hot_min = _positive_int(values["hot_key_min_rows"], "hot_key_min_rows")
    skew = _positive_number(values["skew_ratio"], "skew_ratio")
    if skew > 1:
        raise GSBBadConfigError("skew_ratio must be in (0, 1]", {"skew_ratio": skew})
    memory = _positive_number(values["memory_limit_mib"], "memory_limit_mib")
    scale_value = values["fixed_scale"]
    if isinstance(scale_value, bool) or not isinstance(scale_value, (int, float)):
        raise GSBBadConfigError("fixed_scale must be positive numeric")
    scale = float(scale_value)
    if not math.isfinite(scale) or scale <= 0:
        raise GSBBadConfigError("fixed_scale must be positive and finite")
    tolerance = _positive_number(values["float_tolerance"], "float_tolerance")
    clock_step = _positive_number(values["logical_clock_step_ms"], "logical_clock_step_ms")

    exact_rate = values["exact_cost_ms_per_1k_rows"]
    accel_rate = values["accelerated_cost_ms_per_1k_rows"]
    overhead = values["switch_overhead_ms"]
    exact_rate = (
        _positive_number(exact_rate, "exact_cost_ms_per_1k_rows", allow_zero=True)
        if exact_rate is not None
        else max(clock_step, 1.0)
    )
    accel_rate = (
        _positive_number(accel_rate, "accelerated_cost_ms_per_1k_rows", allow_zero=True)
        if accel_rate is not None
        else exact_rate / 4.0
    )
    overhead = (
        _positive_number(overhead, "switch_overhead_ms", allow_zero=True)
        if overhead is not None
        else 0.0
    )
    if accel_rate > exact_rate:
        raise GSBBadConfigError(
            "accelerated_cost_ms_per_1k_rows cannot exceed exact cost",
            {"exact": exact_rate, "accelerated": accel_rate},
        )

    digest_algorithm = values["digest_algorithm"] or "sha256"
    if not isinstance(digest_algorithm, str) or digest_algorithm.lower() not in {
        "sha256",
        "sha-256",
    }:
        raise GSBBadConfigError(
            "only sha256 content digests are supported",
            {"digest_algorithm": digest_algorithm},
        )

    return EngineConfig(
        time_budget_ms=budget,
        max_batch_rows=max_rows,
        hot_key_min_rows=hot_min,
        skew_ratio=skew,
        memory_limit_mib=memory,
        fixed_scale=int(scale) if float(scale).is_integer() else scale,
        float_tolerance=tolerance,
        logical_clock_step_ms=clock_step,
        exact_cost_ms_per_1k_rows=exact_rate,
        accelerated_cost_ms_per_1k_rows=accel_rate,
        switch_overhead_ms=overhead,
        digest_algorithm="sha256",
        raw=raw,
    )


_AGG_PATTERN = re.compile(
    r"^\s*(sum|avg|mean|var|variance|count|count_non_null|sumsq|sum_sq)\s*\(\s*([^)]+?)\s*\)\s*$",
    re.IGNORECASE,
)


def _normalize_op(op: str) -> str:
    normalized = op.strip().lower().replace("-", "_")
    mapping = {
        "mean": "avg",
        "variance": "var",
        "sum_sq": "sumsq",
        "count_non_null": "count",
        "count_rows": "count",
    }
    value = mapping.get(normalized, normalized)
    if value not in {"sum", "avg", "var", "count", "sumsq"}:
        raise GSBBadQueryError("unsupported aggregate operation", {"op": op})
    return value


def _parse_aggregate(raw: Any, index: int) -> AggregateSpec:
    if isinstance(raw, str):
        match = _AGG_PATTERN.match(raw)
        if not match:
            raise GSBBadQueryError(
                "aggregate expression must use OP(column) syntax",
                {"aggregate_index": index, "expression": raw},
            )
        op = _normalize_op(match.group(1))
        column_text = match.group(2).strip()
        column = None if column_text == "*" else column_text
        if column is None and op != "count":
            raise GSBBadQueryError(
                "only count(*) may omit a column",
                {"aggregate_index": index, "expression": raw},
            )
        name = f"{op}_{column or 'rows'}"
        return AggregateSpec(name=name, op=op, column=column)

    if not isinstance(raw, dict):
        raise GSBBadQueryError(
            "aggregate must be an object or expression string",
            {"aggregate_index": index},
        )
    op_value = raw.get("op", raw.get("type", raw.get("function")))
    if not isinstance(op_value, str):
        raise GSBBadQueryError("aggregate op is required", {"aggregate_index": index})
    op = _normalize_op(op_value)
    column_value = raw.get("column", raw.get("field", raw.get("expr")))
    if isinstance(column_value, str) and column_value.strip() == "*":
        column_value = None
    if column_value is not None and not isinstance(column_value, str):
        raise GSBBadQueryError(
            "aggregate column must be a string",
            {"aggregate_index": index},
        )
    if column_value is None and op != "count":
        raise GSBBadQueryError(
            "aggregate column is required",
            {"aggregate_index": index, "op": op},
        )
    name = raw.get("name", raw.get("as", raw.get("alias")))
    if name is None:
        name = f"{op}_{column_value or 'rows'}"
    if not isinstance(name, str) or not name:
        raise GSBBadQueryError("aggregate name must be a non-empty string")
    return AggregateSpec(name=name, op=op, column=column_value)


def _parse_window(raw: Any) -> WindowSpec | None:
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool):
        raw = {"length": raw, "step": 1}
    if not isinstance(raw, dict):
        raise GSBBadQueryError("window must be an object or positive integer")
    length_value = raw.get("length", raw.get("row_length", raw.get("rows")))
    step_value = raw.get("step", raw.get("stride", 1))
    if isinstance(length_value, bool) or not isinstance(length_value, int) or length_value <= 0:
        raise GSBBadQueryError("window length must be a positive integer")
    if isinstance(step_value, bool) or not isinstance(step_value, int) or step_value <= 0:
        raise GSBBadQueryError("window step must be a positive integer")
    if step_value > length_value:
        raise GSBBadQueryError(
            "window step cannot exceed window length",
            {"length": length_value, "step": step_value},
        )
    return WindowSpec(length=length_value, step=step_value)


def _parse_filter(raw: Any) -> FilterSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise GSBBadQueryError("filter must be an object")
    column = raw.get("column", raw.get("field"))
    if not isinstance(column, str) or not column:
        raise GSBBadQueryError("filter column is required")
    op = str(raw.get("op", raw.get("operator", "eq"))).lower()
    if op not in {"eq", "ne", "in"}:
        raise GSBBadQueryError("unsupported filter operator", {"op": op})
    if "value" not in raw:
        raise GSBBadQueryError("filter value is required")
    return FilterSpec(column=column, op=op, value=raw["value"])


def _parse_query(raw: Any, path: Path) -> QuerySpec:
    if not isinstance(raw, dict):
        raise GSBBadQueryError("query must be an object", {"path": str(path)})
    query_id = raw.get("query_id", raw.get("id", raw.get("name")))
    if not isinstance(query_id, str) or not query_id:
        raise GSBBadQueryError("query_id must be a non-empty string", {"path": str(path)})
    group_key = raw.get("group_key", raw.get("group_by", raw.get("group_column")))
    if isinstance(group_key, list) and len(group_key) == 1:
        group_key = group_key[0]
    if not isinstance(group_key, str) or not group_key:
        raise GSBBadQueryError(
            "exactly one group key column is required",
            {"query_id": query_id},
        )
    aggregates_raw = raw.get("aggregates", raw.get("aggs", raw.get("metrics")))
    if aggregates_raw is None:
        aggregates_raw = ["count(*)"]
    if not isinstance(aggregates_raw, list) or not aggregates_raw:
        raise GSBBadQueryError(
            "aggregates must be a non-empty list",
            {"query_id": query_id},
        )
    aggregates = tuple(_parse_aggregate(item, index) for index, item in enumerate(aggregates_raw))
    names = [agg.name for agg in aggregates]
    if len(names) != len(set(names)):
        raise GSBBadQueryError(
            "aggregate names must be unique",
            {"query_id": query_id, "names": names},
        )
    window = _parse_window(raw.get("window"))
    filter_spec = _parse_filter(raw.get("filter", raw.get("where")))
    return QuerySpec(
        query_id=query_id,
        group_key=group_key,
        aggregates=aggregates,
        window=window,
        filter=filter_spec,
        source=str(path),
    )


def _discover_queries(raw: Any, path: Path) -> list[QuerySpec]:
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict) and isinstance(raw.get("queries"), list):
        items = raw["queries"]
    elif isinstance(raw, dict):
        items = [raw]
    else:
        raise GSBBadQueryError("query file must contain an object or list", {"path": str(path)})
    return [_parse_query(item, path) for item in items]


def _columns_from_raw(raw: Any, batch_index: int) -> dict[str, list[Any]]:
    if isinstance(raw, dict):
        columns: dict[str, list[Any]] = {}
        for name, values in raw.items():
            if not isinstance(name, str) or not name:
                raise GSBBadBatchError(
                    "column names must be non-empty strings",
                    {"batch_index": batch_index},
                )
            if name in columns:
                raise GSBBadBatchError(
                    "duplicate column name",
                    {"batch_index": batch_index, "column": name},
                )
            if not isinstance(values, list):
                raise GSBBadBatchError(
                    "column values must be an array",
                    {"batch_index": batch_index, "column": name},
                )
            columns[name] = values
        return columns
    if isinstance(raw, list):
        columns = {}
        for item in raw:
            if not isinstance(item, dict):
                raise GSBBadBatchError(
                    "column list entries must be objects",
                    {"batch_index": batch_index},
                )
            name = item.get("name")
            values = item.get("values", item.get("data"))
            if not isinstance(name, str) or not name:
                raise GSBBadBatchError(
                    "column name is required",
                    {"batch_index": batch_index},
                )
            if name in columns:
                raise GSBBadBatchError(
                    "duplicate column name",
                    {"batch_index": batch_index, "column": name},
                )
            if not isinstance(values, list):
                raise GSBBadBatchError(
                    "column values must be an array",
                    {"batch_index": batch_index, "column": name},
                )
            columns[name] = values
        return columns
    raise GSBBadBatchError(
        "columns must be an object or array",
        {"batch_index": batch_index},
    )


def _digest_candidates(batch_index: int, row_count: int, columns: dict[str, list[Any]]) -> set[str]:
    full = {"batch_index": batch_index, "row_count": row_count, "columns": columns}
    without_index = {"row_count": row_count, "columns": columns}
    column_list = [{"name": name, "values": columns[name]} for name in sorted(columns)]
    candidates = {
        sha256_json(full),
        sha256_json(without_index),
        sha256_json(columns),
        sha256_json(column_list),
        sha256_bytes(canonical_json(full).encode("utf-8")),
    }
    return candidates


def parse_batch(raw: Any, path: Path, ordinal: int, config: EngineConfig) -> ColumnBatch:
    if not isinstance(raw, dict):
        raise GSBBadBatchError("batch root must be an object", {"path": str(path)})
    index_value = raw.get("batch_index", raw.get("batch_id", raw.get("sequence", raw.get("seq"))))
    if isinstance(index_value, bool) or not isinstance(index_value, int):
        raise GSBBadBatchError(
            "batch_index must be an integer",
            {"path": str(path), "batch_index": index_value},
        )
    row_count_value = raw.get("row_count", raw.get("rows", raw.get("num_rows")))
    if isinstance(row_count_value, bool) or not isinstance(row_count_value, int):
        raise GSBBadBatchError(
            "row_count must be an integer",
            {"path": str(path), "batch_index": index_value},
        )
    if row_count_value < 0:
        raise GSBBadBatchError(
            "row_count must be non-negative",
            {"path": str(path), "batch_index": index_value},
        )
    if row_count_value > config.max_batch_rows:
        raise GSBBadBatchError(
            "batch exceeds max_batch_rows",
            {
                "path": str(path),
                "batch_index": index_value,
                "row_count": row_count_value,
                "max_batch_rows": config.max_batch_rows,
            },
        )
    columns_raw = raw.get("columns", raw.get("data"))
    columns = _columns_from_raw(columns_raw, index_value)
    if not columns and row_count_value:
        raise GSBBadBatchError(
            "non-empty batch has no columns",
            {"path": str(path), "batch_index": index_value},
        )
    for name, values in columns.items():
        if len(values) != row_count_value:
            raise GSBBadBatchError(
                "column length does not match row_count",
                {
                    "path": str(path),
                    "batch_index": index_value,
                    "column": name,
                    "column_length": len(values),
                    "row_count": row_count_value,
                },
            )
    digest_value = raw.get(
        "digest",
        raw.get("content_digest", raw.get("checksum", raw.get("summary"))),
    )
    if not isinstance(digest_value, str) or not digest_value:
        raise GSBBadBatchError(
            "content digest is required",
            {"path": str(path), "batch_index": index_value},
        )
    normalized = normalize_digest(digest_value)
    candidates = _digest_candidates(index_value, row_count_value, columns)
    if normalized not in candidates:
        raise GSBBadBatchError(
            "content digest does not match column content",
            {
                "path": str(path),
                "batch_index": index_value,
                "declared_digest": digest_value,
                "canonical_digest": sha256_json(
                    {
                        "batch_index": index_value,
                        "row_count": row_count_value,
                        "columns": columns,
                    }
                ),
            },
        )
    return ColumnBatch(
        ordinal=ordinal,
        batch_index=index_value,
        row_count=row_count_value,
        columns=columns,
        digest=normalized,
        source_path=str(path),
        actual_digest=sha256_json(
            {
                "batch_index": index_value,
                "row_count": row_count_value,
                "columns": columns,
            }
        ),
    )


def _column_class(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if is_number(value):
        return "number"
    if isinstance(value, str):
        return "str"
    return "unsupported"


def _validate_references(
    queries: tuple[QuerySpec, ...],
    batches: tuple[ColumnBatch, ...],
) -> None:
    for query in queries:
        numeric_columns: set[str] = set()
        for agg in query.aggregates:
            if agg.column is not None and agg.op in {"sum", "avg", "var", "sumsq"}:
                numeric_columns.add(agg.column)
        for batch in batches:
            if query.group_key not in batch.columns:
                raise GSBBadQueryError(
                    "group key column is missing from a batch",
                    {
                        "query_id": query.query_id,
                        "batch_index": batch.batch_index,
                        "column": query.group_key,
                    },
                )
            for agg in query.aggregates:
                if agg.column is not None and agg.column not in batch.columns:
                    raise GSBBadQueryError(
                        "aggregate column is missing from a batch",
                        {
                            "query_id": query.query_id,
                            "batch_index": batch.batch_index,
                            "column": agg.column,
                            "aggregate": agg.name,
                        },
                    )
            if query.filter is not None and query.filter.column not in batch.columns:
                raise GSBBadQueryError(
                    "filter column is missing from a batch",
                    {
                        "query_id": query.query_id,
                        "batch_index": batch.batch_index,
                        "column": query.filter.column,
                    },
                )

        group_classes: set[str] = set()
        for batch in batches:
            for value in batch.columns[query.group_key]:
                if value is None:
                    raise GSBBadBatchError(
                        "group key contains null",
                        {
                            "batch_index": batch.batch_index,
                            "column": query.group_key,
                        },
                    )
                klass = _column_class(value)
                if klass == "unsupported":
                    raise GSBBadBatchError(
                        "group key contains unsupported type",
                        {
                            "batch_index": batch.batch_index,
                            "column": query.group_key,
                            "type": json_type_tag(value),
                        },
                    )
                if klass == "number" and not is_finite_number(value):
                    raise GSBBadBatchError(
                        "group key contains NaN or infinity",
                        {
                            "batch_index": batch.batch_index,
                            "column": query.group_key,
                            "value": str(value),
                        },
                    )
                group_classes.add(klass)
                group_key_token(value)
        if "str" in group_classes and len(group_classes) > 1:
            raise GSBBadQueryError(
                "group key column has incomparable mixed types",
                {"query_id": query.query_id, "types": sorted(group_classes)},
            )
        if "bool" in group_classes and group_classes != {"bool"}:
            raise GSBBadQueryError(
                "boolean group keys cannot mix with other types",
                {"query_id": query.query_id, "types": sorted(group_classes)},
            )

        for column in numeric_columns:
            for batch in batches:
                for value in batch.columns[column]:
                    if value is not None and not is_number(value):
                        raise GSBBadQueryError(
                            "numeric aggregate column contains non-numeric value",
                            {
                                "query_id": query.query_id,
                                "batch_index": batch.batch_index,
                                "column": column,
                                "type": json_type_tag(value),
                            },
                        )
        if query.filter is not None:
            filter_value = query.filter.value
            filter_type = _column_class(filter_value)
            if filter_type == "unsupported":
                raise GSBBadQueryError(
                    "unsupported filter value type",
                    {"query_id": query.query_id},
                )
            for batch in batches:
                seen_types = {
                    _column_class(value)
                    for value in batch.columns[query.filter.column]
                    if value is not None
                }
                if seen_types and filter_type != "number" and filter_type not in seen_types:
                    raise GSBBadQueryError(
                        "filter value type is not comparable with column",
                        {
                            "query_id": query.query_id,
                            "batch_index": batch.batch_index,
                            "filter_type": filter_type,
                            "column_types": sorted(seen_types),
                        },
                    )


def _find_config_path(root: Path) -> Path:
    candidates = [
        root / "config.json",
        root / "execution_config.json",
        root / "engine_config.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    matches = sorted(root.glob("*config*.json"))
    if len(matches) == 1:
        return matches[0]
    raise GSBUsageError(
        "cannot locate execution config",
        {"root": str(root), "candidates": [str(path) for path in candidates]},
    )


def _find_query_paths(root: Path) -> tuple[Path, ...]:
    candidates = [
        root / "queries.json",
        root / "query.json",
        root / "aggregation_queries.json",
    ]
    found = [path for path in candidates if path.is_file()]
    if found:
        return tuple(found)
    query_dir = root / "queries"
    if query_dir.is_dir():
        found = sorted(query_dir.glob("*.json"))
    if not found:
        raise GSBUsageError("cannot locate query description", {"root": str(root)})
    return tuple(found)


def _find_batch_paths(root: Path) -> tuple[Path, ...]:
    candidates = [root / "batches", root / "batch"]
    for directory in candidates:
        if directory.is_dir():
            files = sorted(path for path in directory.rglob("*.json") if path.is_file())
            if files:
                return tuple(files)
    files = sorted(
        path
        for path in root.glob("batch*.json")
        if path.is_file() and "config" not in path.name and "quer" not in path.name
    )
    if not files:
        raise GSBUsageError("cannot locate columnar batch files", {"root": str(root)})
    return tuple(files)


def load_sample(root: Path) -> Sample:
    root = root.resolve()
    if not root.is_dir():
        raise GSBUsageError("sample directory does not exist", {"root": str(root)})
    config_path = _find_config_path(root)
    try:
        config_raw = load_json(config_path)
    except GSBError as exc:
        raise GSBBadConfigError(
            "execution config JSON is invalid",
            {"path": str(config_path), "cause": str(exc)},
        ) from exc
    config = parse_config(config_raw, config_path)
    query_paths = _find_query_paths(root)
    queries_list: list[QuerySpec] = []
    for path in query_paths:
        try:
            query_raw = load_json(path)
        except GSBError as exc:
            raise GSBBadQueryError(
                "query JSON is invalid",
                {"path": str(path), "cause": str(exc)},
            ) from exc
        queries_list.extend(_discover_queries(query_raw, path))
    query_ids = [query.query_id for query in queries_list]
    if len(query_ids) != len(set(query_ids)):
        raise GSBBadQueryError("query ids must be unique", {"query_ids": query_ids})
    queries = tuple(queries_list)

    batch_paths = _find_batch_paths(root)
    parsed: list[ColumnBatch] = []
    for ordinal, path in enumerate(batch_paths, start=1):
        try:
            batch_raw = load_json(path)
        except GSBError as exc:
            raise GSBBadBatchError(
                "batch JSON is invalid",
                {"path": str(path), "cause": str(exc)},
            ) from exc
        parsed.append(parse_batch(batch_raw, path, ordinal, config))
    parsed.sort(key=lambda batch: batch.batch_index)
    for left, right in zip(parsed, parsed[1:]):
        if left.batch_index >= right.batch_index:
            raise GSBBadBatchError(
                "batch indexes must be strictly increasing",
                {"left": left.batch_index, "right": right.batch_index},
            )
    batches = tuple(
        ColumnBatch(
            ordinal=ordinal,
            batch_index=batch.batch_index,
            row_count=batch.row_count,
            columns=batch.columns,
            digest=batch.digest,
            source_path=batch.source_path,
            actual_digest=batch.actual_digest,
        )
        for ordinal, batch in enumerate(parsed, start=1)
    )
    _validate_references(queries, batches)
    fingerprint = sha256_json(
        {
            "config": config.to_dict(),
            "queries": [query.to_dict() for query in queries],
            "batches": [
                {
                    "ordinal": batch.ordinal,
                    "batch_index": batch.batch_index,
                    "row_count": batch.row_count,
                    "digest": batch.digest,
                    "actual_digest": batch.actual_digest,
                }
                for batch in batches
            ],
        }
    )
    return Sample(
        root=root,
        config=config,
        queries=queries,
        batches=batches,
        fingerprint=fingerprint,
        config_path=config_path,
        query_paths=query_paths,
        batch_paths=batch_paths,
    )

