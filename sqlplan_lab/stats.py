"""Statistics snapshot construction and deterministic cardinality estimation."""

from __future__ import annotations

import bisect
import hashlib
import sqlite3
from typing import Any

from .errors import E_STATS, LabError
from .util import (
    canonical_json,
    json_digest,
    round_half_up,
    sql_quote_identifier,
    typed_value,
)


DEFAULT_PARAMETERS = {
    "histogram_buckets": 8,
    "sample_rate": 1.0,
    "null_fraction_overrides": {},
}


def normalize_parameters(
    histogram_buckets: int = 8,
    sample_rate: float = 1.0,
    null_fraction_overrides: dict[str, float] | None = None,
) -> dict[str, Any]:
    buckets = int(histogram_buckets)
    rate = float(sample_rate)
    overrides = dict(sorted((null_fraction_overrides or {}).items()))
    if buckets < 1 or buckets > 128:
        raise LabError(E_STATS, "histogram_buckets must be between 1 and 128")
    if not 0.0 < rate <= 1.0:
        raise LabError(E_STATS, "sample_rate must be in (0, 1]")
    normalized_overrides: dict[str, float] = {}
    for key, value in overrides.items():
        text = key.lower().strip()
        if "." not in text:
            raise LabError(E_STATS, "null override keys must be table.column")
        fraction = float(value)
        if fraction < 0.0 or fraction > 1.0:
            raise LabError(E_STATS, "null fraction must be in [0, 1]")
        normalized_overrides[text] = fraction
    return {
        "histogram_buckets": buckets,
        "sample_rate": rate,
        "null_fraction_overrides": normalized_overrides,
    }


def _stats_error(message: str, **details: Any) -> None:
    raise LabError(E_STATS, message, details)


def _value_key(value: Any) -> str:
    return canonical_json(typed_value(value))


def _sample_rows(rows: list[tuple[Any, ...]], rate: float) -> list[tuple[Any, ...]]:
    if not rows:
        return []
    serialized = [
        (canonical_json([typed_value(value) for value in row]), index)
        for index, row in enumerate(rows)
    ]
    serialized.sort(key=lambda item: item[0])
    if rate >= 1.0:
        return [rows[index] for _, index in serialized]
    desired = max(1, round_half_up(len(rows) * rate))
    scored = []
    for text, index in serialized:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        scored.append((digest, text, index))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [rows[index] for _, _, index in scored[:desired]]


def _numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _column_stats(
    values: list[Any],
    sample_size: int,
    row_count: int,
    rate: float,
    buckets: int,
    null_override: float | None,
) -> dict[str, Any]:
    nonnull = [value for value in values if value is not None]
    null_fraction = (
        float(null_override)
        if null_override is not None
        else (len(values) - len(nonnull)) / sample_size if sample_size else 0.0
    )
    distinct_keys = {_value_key(value) for value in nonnull}
    if nonnull:
        scaled_distinct = round_half_up(len(distinct_keys) / rate)
        nonnull_row_estimate = max(1, round_half_up(row_count * (1.0 - null_fraction)))
        ndv = min(nonnull_row_estimate, max(1, scaled_distinct))
    else:
        ndv = 0

    counts: dict[str, int] = {}
    values_by_key: dict[str, Any] = {}
    for value in nonnull:
        key = _value_key(value)
        counts[key] = counts.get(key, 0) + 1
        values_by_key[key] = value
    most_common = []
    for key, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:12]:
        most_common.append(
            {
                "value": values_by_key[key],
                "value_key": key,
                "share_ppm": round_half_up(count * 1_000_000 / len(nonnull)),
            }
        )

    all_numeric = bool(nonnull) and all(_numeric(value) for value in nonnull)
    histogram: dict[str, Any]
    if not nonnull:
        histogram = {"kind": "empty", "boundaries": [], "counts": []}
    elif all_numeric:
        numeric_values = [float(value) for value in nonnull]
        minimum = min(numeric_values)
        maximum = max(numeric_values)
        boundaries: list[float] = []
        for index in range(buckets + 1):
            if maximum == minimum:
                boundaries.append(minimum)
            else:
                boundaries.append(minimum + (maximum - minimum) * index / buckets)
        bucket_counts = [0 for _ in range(buckets)]
        for value in numeric_values:
            if maximum == minimum:
                bucket = 0
            else:
                bucket = int((value - minimum) / (maximum - minimum) * buckets)
                bucket = min(buckets - 1, max(0, bucket))
            bucket_counts[bucket] += 1
        histogram = {
            "kind": "numeric",
            "boundaries": boundaries,
            "counts": bucket_counts,
            "non_null_sample_count": len(nonnull),
        }
    else:
        ordered = sorted(nonnull, key=lambda value: str(value))
        boundaries = []
        for index in range(buckets + 1):
            position = min(len(ordered) - 1, int(round(index * (len(ordered) - 1) / buckets)))
            boundaries.append(ordered[position])
        bucket_counts = [0 for _ in range(buckets)]
        boundary_strings = [str(value) for value in boundaries]
        for value in nonnull:
            index = bisect.bisect_right(boundary_strings, str(value)) - 1
            index = min(buckets - 1, max(0, index))
            bucket_counts[index] += 1
        histogram = {
            "kind": "text",
            "boundaries": boundaries,
            "counts": bucket_counts,
            "non_null_sample_count": len(nonnull),
        }
    return {
        "ndv": int(ndv),
        "null_fraction": float(null_fraction),
        "histogram": histogram,
        "most_common": most_common,
        "sample_non_null_count": len(nonnull),
    }


def build_stats_payload(
    conn: sqlite3.Connection,
    data_version: str,
    tables_meta: list[dict[str, Any]],
    parameters: dict[str, Any],
) -> dict[str, Any]:
    params = normalize_parameters(
        parameters.get("histogram_buckets", 8),
        parameters.get("sample_rate", 1.0),
        parameters.get("null_fraction_overrides", {}),
    )
    rate = params["sample_rate"]
    buckets = params["histogram_buckets"]
    overrides = params["null_fraction_overrides"]
    stats_tables: dict[str, Any] = {}
    for table_meta in sorted(tables_meta, key=lambda item: item["table"]):
        table = table_meta["table"]
        columns = table_meta["schema"]["columns"]
        cursor = conn.execute(f"SELECT COUNT(*) FROM {sql_quote_identifier(table)}")
        row_count = int(cursor.fetchone()[0])
        cursor = conn.execute(f"SELECT * FROM {sql_quote_identifier(table)}")
        rows = [tuple(row) for row in cursor.fetchall()]
        sampled = _sample_rows(rows, rate)
        column_stats: dict[str, Any] = {}
        for index, column in enumerate(columns):
            name = column["name"]
            values = [row[index] for row in sampled]
            key = f"{table}.{name}"
            column_stats[name] = _column_stats(
                values,
                sample_size=len(sampled),
                row_count=row_count,
                rate=rate,
                buckets=buckets,
                null_override=overrides.get(key),
            )
        stats_tables[table] = {
            "row_count": row_count,
            "sample_size": len(sampled),
            "columns": column_stats,
        }
    core = {
        "format": 1,
        "data_version": data_version,
        "parameters": params,
        "tables": stats_tables,
    }
    version = "sv_" + json_digest(core)[:24]
    payload = {**core, "stats_version": version}
    return payload


def validate_stats_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _stats_error("statistics payload is not an object")
    if payload.get("format") != 1:
        _stats_error("unsupported statistics payload format")
    if not isinstance(payload.get("data_version"), str):
        _stats_error("statistics payload has no data_version")
    if not isinstance(payload.get("stats_version"), str):
        _stats_error("statistics payload has no stats_version")
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        _stats_error("statistics payload has no tables object")
    for table, table_stats in tables.items():
        if not isinstance(table, str) or not isinstance(table_stats, dict):
            _stats_error("malformed table statistics", table=table)
        if not isinstance(table_stats.get("row_count"), int):
            _stats_error("table statistics has no integer row_count", table=table)
        columns = table_stats.get("columns")
        if not isinstance(columns, dict):
            _stats_error("table statistics has no columns", table=table)
        for column, column_stats in columns.items():
            if not isinstance(column_stats, dict):
                _stats_error("malformed column statistics", table=table, column=column)
            if not isinstance(column_stats.get("ndv"), int):
                _stats_error("column statistics has no integer ndv", table=table, column=column)
            if not isinstance(column_stats.get("null_fraction"), (int, float)):
                _stats_error(
                    "column statistics has no null_fraction", table=table, column=column
                )
            histogram = column_stats.get("histogram")
            if not isinstance(histogram, dict) or "boundaries" not in histogram or "counts" not in histogram:
                _stats_error("column statistics has no histogram", table=table, column=column)
    return payload


def get_plan_alias_map(plan: dict[str, Any]) -> dict[str, str]:
    return {item["alias"]: item["table"] for item in plan["tables"]}


def _resolve_plan_column(
    column: dict[str, Any],
    alias_map: dict[str, str],
    stats_tables: dict[str, Any],
) -> tuple[str, str, dict[str, Any]]:
    name = column.get("name")
    qualifier = column.get("qualifier")
    if not isinstance(name, str):
        _stats_error("malformed column reference")
    if qualifier is not None:
        table = alias_map.get(qualifier)
        if table is None:
            _stats_error("column alias is absent from statistics", alias=qualifier)
    else:
        matches = []
        for table, table_stats in stats_tables.items():
            if name in table_stats.get("columns", {}):
                matches.append(table)
        matches = sorted(set(matches))
        if len(matches) != 1:
            _stats_error("cannot resolve unqualified column in statistics", column=name)
        table = matches[0]
    table_stats = stats_tables.get(table)
    if table_stats is None:
        _stats_error("missing table statistics", table=table)
    column_stats = table_stats.get("columns", {}).get(name)
    if column_stats is None:
        _stats_error("missing column statistics", table=table, column=name)
    return table, name, column_stats


def _nonnull_fraction(column_stats: dict[str, Any]) -> float:
    return max(0.0, min(1.0, 1.0 - float(column_stats.get("null_fraction", 0.0))))


def _most_common_share(column_stats: dict[str, Any], value: Any) -> float | None:
    key = _value_key(value)
    for item in column_stats.get("most_common", []):
        if item.get("value_key") == key:
            return float(item.get("share_ppm", 0)) / 1_000_000.0
    return None


def _top_share_sum(column_stats: dict[str, Any]) -> float:
    return min(
        1.0,
        sum(float(item.get("share_ppm", 0)) for item in column_stats.get("most_common", []))
        / 1_000_000.0,
    )


def _equality_nonnull_share(column_stats: dict[str, Any], value: Any) -> float:
    known = _most_common_share(column_stats, value)
    if known is not None:
        return max(0.0, min(1.0, known))
    ndv = int(column_stats.get("ndv", 0))
    if ndv <= 0:
        return 0.0
    remaining_share = max(0.0, 1.0 - _top_share_sum(column_stats))
    remaining_distinct = max(1, ndv - len(column_stats.get("most_common", [])))
    return remaining_share / remaining_distinct


def _compare_values(left: Any, right: Any, op: str) -> bool:
    try:
        if op == "=":
            return left == right
        if op in {"!=", "<>"}:
            return left != right
        if op == "<":
            return left < right
        if op == "<=":
            return left <= right
        if op == ">":
            return left > right
        if op == ">=":
            return left >= right
    except TypeError:
        return False
    return False


def _histogram_cdf_below(histogram: dict[str, Any], value: Any, inclusive: bool) -> float:
    kind = histogram.get("kind")
    counts = histogram.get("counts", [])
    total = float(histogram.get("non_null_sample_count") or sum(counts))
    if total <= 0 or not counts:
        return 0.0
    boundaries = histogram.get("boundaries", [])
    if len(boundaries) != len(counts) + 1:
        return 0.0
    if kind == "numeric":
        try:
            threshold = float(value)
        except (TypeError, ValueError):
            return 0.0
        if threshold < float(boundaries[0]):
            return 0.0
        if threshold > float(boundaries[-1]) or (inclusive and threshold == float(boundaries[-1])):
            return 1.0
        accumulated = 0.0
        for index, count in enumerate(counts):
            low = float(boundaries[index])
            high = float(boundaries[index + 1])
            if threshold <= low:
                break
            if threshold >= high and not (inclusive and index == len(counts) - 1):
                accumulated += count
                continue
            if high > low:
                fraction = min(1.0, max(0.0, (threshold - low) / (high - low)))
                if inclusive and threshold == low:
                    fraction = 0.0
                accumulated += count * fraction
            elif threshold >= high:
                accumulated += count
            break
        return min(1.0, max(0.0, accumulated / total))
    threshold_text = str(value)
    accumulated = 0.0
    for index, count in enumerate(counts):
        low = str(boundaries[index])
        high = str(boundaries[index + 1])
        if threshold_text <= low:
            break
        if threshold_text > high or (inclusive and threshold_text == high):
            accumulated += count
            continue
        accumulated += count * 0.5
        break
    return min(1.0, max(0.0, accumulated / total))


def _literal_range_share(
    column_stats: dict[str, Any], op: str, value: Any
) -> float:
    nonnull = _nonnull_fraction(column_stats)
    histogram = column_stats["histogram"]
    below = _histogram_cdf_below(histogram, value, inclusive=False)
    at_or_below = _histogram_cdf_below(histogram, value, inclusive=True)
    if op == "<":
        share = below
    elif op == "<=":
        share = at_or_below
    elif op == ">":
        share = 1.0 - at_or_below
    elif op == ">=":
        share = 1.0 - below
    else:
        share = 0.0
    return max(0.0, min(1.0, nonnull * share))


def _condition_selectivity(
    condition: dict[str, Any] | None,
    alias_map: dict[str, str],
    stats_tables: dict[str, Any],
) -> float:
    if condition is None:
        return 1.0
    kind = condition.get("kind")
    if kind == "bool":
        op = condition.get("op")
        children = condition.get("children", [])
        if not isinstance(children, list) or not children:
            _stats_error("malformed boolean statistics: empty children")
        selectivities = [
            _condition_selectivity(child, alias_map, stats_tables) for child in children
        ]
        if op == "and":
            result = 1.0
            for value in selectivities:
                result *= value
            return max(0.0, min(1.0, result))
        if op == "or":
            remaining = 1.0
            for value in selectivities:
                remaining *= 1.0 - value
            return max(0.0, min(1.0, 1.0 - remaining))
        _stats_error("unknown boolean operator in plan", op=op)
    if kind == "in":
        _, _, column_stats = _resolve_plan_column(
            condition.get("column", {}), alias_map, stats_tables
        )
        nonnull = _nonnull_fraction(column_stats)
        values = condition.get("values", [])
        if not isinstance(values, list):
            _stats_error("malformed IN predicate in plan")
        seen: set[str] = set()
        share = 0.0
        for literal in values:
            value = literal.get("value")
            if value is None:
                continue
            key = _value_key(value)
            if key in seen:
                continue
            seen.add(key)
            share += _equality_nonnull_share(column_stats, value)
        return max(0.0, min(1.0, nonnull * min(1.0, share)))
    if kind != "comparison":
        _stats_error("unknown predicate kind in plan", predicate_kind=kind)
    left = condition.get("left", {})
    right = condition.get("right", {})
    op = condition.get("op")
    _, _, left_stats = _resolve_plan_column(left, alias_map, stats_tables)
    if right.get("kind") == "column":
        _, _, right_stats = _resolve_plan_column(right, alias_map, stats_tables)
        left_nonnull = _nonnull_fraction(left_stats)
        right_nonnull = _nonnull_fraction(right_stats)
        if left.get("qualifier") == right.get("qualifier") and left.get("name") == right.get("name"):
            equality_share = 1.0
        else:
            denominator = max(
                1,
                int(left_stats.get("ndv", 0)),
                int(right_stats.get("ndv", 0)),
            )
            equality_share = 1.0 / denominator
        if op == "=":
            return max(0.0, min(1.0, left_nonnull * right_nonnull * equality_share))
        if op in {"!=", "<>"}:
            return max(0.0, min(1.0, left_nonnull * right_nonnull * (1.0 - equality_share)))
        return max(0.0, min(1.0, left_nonnull * right_nonnull * (1.0 / 3.0)))
    if right.get("kind") != "literal":
        _stats_error("malformed comparison right operand")
    value = right.get("value")
    if value is None:
        return 0.0
    if op == "=":
        return max(
            0.0,
            min(1.0, _nonnull_fraction(left_stats) * _equality_nonnull_share(left_stats, value)),
        )
    if op in {"!=", "<>"}:
        equality = _nonnull_fraction(left_stats) * _equality_nonnull_share(left_stats, value)
        return max(0.0, min(1.0, _nonnull_fraction(left_stats) - equality))
    if op in {"<", "<=", ">", ">="}:
        return _literal_range_share(left_stats, op, value)
    _stats_error("unknown comparison operator in plan", op=op)


def estimate_plan(plan: dict[str, Any], stats_payload: dict[str, Any]) -> dict[str, Any]:
    stats = validate_stats_payload(stats_payload)
    stats_tables = stats["tables"]
    alias_map = get_plan_alias_map(plan)
    nodes = {node["op_id"]: node for node in plan["nodes"]}
    estimates: dict[str, int] = {}
    details: dict[str, dict[str, Any]] = {}
    order = plan.get("topological_order", [node["op_id"] for node in plan["nodes"]])
    for op_id in order:
        node = nodes[op_id]
        kind = node["kind"]
        if kind == "scan":
            table = node["table"]
            table_stats = stats_tables.get(table)
            if table_stats is None:
                _stats_error("missing table statistics", table=table)
            estimated = int(table_stats["row_count"])
            estimates[op_id] = estimated
            details[op_id] = {
                "input_rows": None,
                "base_rows": estimated,
                "selectivity_ppm": 1_000_000,
                "estimated_rows": estimated,
                "statistics": {"table": table, "row_count": estimated},
            }
            continue
        if kind == "join":
            left_id, right_id = node["children"]
            left_rows = estimates[left_id]
            right_rows = estimates[right_id]
            selectivity = _condition_selectivity(node.get("predicate"), alias_map, stats_tables)
            estimated = round_half_up(left_rows * right_rows * selectivity)
            estimates[op_id] = estimated
            details[op_id] = {
                "input_rows": left_rows + right_rows,
                "left_rows": left_rows,
                "right_rows": right_rows,
                "base_rows": left_rows * right_rows,
                "selectivity_ppm": round_half_up(selectivity * 1_000_000),
                "estimated_rows": estimated,
            }
            continue
        if kind == "filter":
            input_id = node["children"][0]
            input_rows = estimates[input_id]
            selectivity = _condition_selectivity(node.get("predicate"), alias_map, stats_tables)
            estimated = round_half_up(input_rows * selectivity)
            estimates[op_id] = estimated
            details[op_id] = {
                "input_rows": input_rows,
                "base_rows": input_rows,
                "selectivity_ppm": round_half_up(selectivity * 1_000_000),
                "estimated_rows": estimated,
            }
            continue
        _stats_error("unknown logical operator", operator=kind)
    root = plan["root"]
    return {
        "stats_version": stats["stats_version"],
        "data_version": stats["data_version"],
        "plan_id": plan["plan_id"],
        "root_estimated_rows": estimates[root],
        "operators": [
            {
                "op_id": op_id,
                "kind": nodes[op_id]["kind"],
                "estimated_rows": estimates[op_id],
                **details[op_id],
            }
            for op_id in order
        ],
    }


def stats_checksum(payload: dict[str, Any]) -> str:
    return json_digest(payload)
