from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from .errors import fail
from .expressions import eval_predicate, eval_value_expr, value_expr_refs
from .model import Dataset, ExecutionConfig, Relation
from .util import canonical_json, json_key


EPS = float(np.finfo(np.float64).eps)
INT64_MIN = int(np.iinfo(np.int64).min)
INT64_MAX = int(np.iinfo(np.int64).max)
ROUNDING_MODE = "ROUND_HALF_AWAY_FROM_ZERO"
NAN_POLICY = "IGNORE_AND_COUNT"
SUM_ORDER = "SHARD_ID_THEN_STABLE_ROW_ORDER"


def dtype_name(array: np.ndarray) -> str:
    if array.dtype == np.bool_:
        return "bool"
    if array.dtype == np.int64:
        return "int64"
    if array.dtype == np.float64:
        return "float64"
    if array.dtype.kind == "i":
        return "int64"
    if array.dtype.kind == "f":
        return "float64"
    if array.dtype.kind == "b":
        return "bool"
    fail("E_STATE", f"unsupported relation dtype {array.dtype}")


def _numpy_dtype(name: str) -> np.dtype[Any]:
    return {
        "bool": np.dtype(np.bool_),
        "int64": np.dtype(np.int64),
        "float64": np.dtype(np.float64),
    }[name]


def _json_scalar(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


def relation_to_wire(relation: Relation) -> dict[str, Any]:
    return {
        "row_count": relation.row_count,
        "global_start": relation.global_start,
        "columns": {
            name: {
                "dtype": dtype_name(array),
                "values": [_json_scalar(value) for value in array.tolist()],
            }
            for name, array in relation.columns.items()
        },
        "error_bounds": {
            name: {"dtype": "float64", "values": [_json_scalar(value) for value in array.tolist()]}
            for name, array in relation.error_bounds.items()
        },
        "error_sources": relation.error_sources,
        "source_shards": relation.source_shards,
    }


def relation_from_wire(payload: dict[str, Any]) -> Relation:
    columns: dict[str, np.ndarray] = {}
    for name, spec in payload["columns"].items():
        columns[name] = np.asarray(spec["values"], dtype=_numpy_dtype(spec["dtype"]))
    error_bounds = {
        name: np.asarray(spec["values"], dtype=np.float64)
        for name, spec in payload.get("error_bounds", {}).items()
    }
    return Relation(
        columns=columns,
        row_count=int(payload["row_count"]),
        global_start=int(payload.get("global_start", 0)),
        error_bounds=error_bounds,
        error_sources={
            name: list(values) for name, values in payload.get("error_sources", {}).items()
        },
        source_shards=[int(value) for value in payload.get("source_shards", [])],
    )


def relation_bytes(relation: Relation) -> int:
    total = 0
    for array in relation.columns.values():
        total += int(array.nbytes)
    for array in relation.error_bounds.values():
        total += int(array.nbytes)
    return total


def empty_relation(schema: dict[str, str]) -> Relation:
    return Relation(
        columns={name: np.asarray([], dtype=_numpy_dtype(dtype)) for name, dtype in schema.items()},
        row_count=0,
        error_bounds={name: np.asarray([], dtype=np.float64) for name in schema},
    )


def apply_scan(
    dataset: Dataset,
    scan_node: dict[str, Any],
    start: int,
    end: int,
    shard_id: int,
) -> Relation:
    relation = dataset.slice(start, end)
    relation.columns = {name: relation.columns[name] for name in scan_node["columns"]}
    predicates = scan_node.get("pushed_predicates", [])
    if predicates:
        mask = np.ones(relation.row_count, dtype=bool)
        for predicate in predicates:
            mask &= eval_predicate(predicate, relation.columns, relation.row_count)
        relation = filter_by_mask(relation, mask)
    relation.source_shards = [shard_id]
    for name, array in relation.columns.items():
        if array.dtype.kind in {"f", "i"}:
            relation.error_bounds[name] = np.zeros(relation.row_count, dtype=np.float64)
            relation.error_sources[name] = [f"scan:{name}", "scan_error=0"]
    return relation


def filter_by_mask(relation: Relation, mask: np.ndarray) -> Relation:
    mask = np.asarray(mask, dtype=bool)
    if mask.size != relation.row_count:
        fail("E_STATE", "filter mask length does not match relation row count")
    return Relation(
        columns={name: array[mask].copy() for name, array in relation.columns.items()},
        row_count=int(np.count_nonzero(mask)),
        global_start=relation.global_start,
        error_bounds={
            name: array[mask].copy() for name, array in relation.error_bounds.items()
        },
        error_sources={name: list(values) for name, values in relation.error_sources.items()},
        source_shards=list(relation.source_shards),
    )


def apply_filter(relation: Relation, predicate: dict[str, Any]) -> Relation:
    mask = eval_predicate(predicate, relation.columns, relation.row_count)
    return filter_by_mask(relation, mask)


def apply_project(relation: Relation, columns: list[dict[str, Any]]) -> Relation:
    output: dict[str, np.ndarray] = {}
    bounds: dict[str, np.ndarray] = {}
    sources: dict[str, list[str]] = {}
    for item in columns:
        name = item["name"]
        value = eval_value_expr(item["expr"], relation.columns, relation.row_count)
        output[name] = np.asarray(value)
        expr = item["expr"]
        refs = sorted(value_expr_refs(expr))
        if len(refs) == 1 and "column" in expr:
            bounds[name] = relation.error_bounds.get(refs[0], np.zeros(relation.row_count))
            sources[name] = list(relation.error_sources.get(refs[0], []))
        else:
            upstream = np.zeros(relation.row_count, dtype=np.float64)
            for ref in refs:
                upstream += relation.error_bounds.get(ref, np.zeros(relation.row_count))
            bounds[name] = upstream
            chain: list[str] = []
            for ref in refs:
                chain.extend(relation.error_sources.get(ref, []))
            sources[name] = chain + ["project_float64_rounding"]
    return Relation(
        columns=output,
        row_count=relation.row_count,
        global_start=relation.global_start,
        error_bounds=bounds,
        error_sources=sources,
        source_shards=list(relation.source_shards),
    )


def fixed_convert(
    values: np.ndarray,
    scale: int,
    *,
    node_id: str,
    field: str,
) -> np.ndarray:
    values = np.asarray(values)
    numeric = values.astype(np.float64, copy=False)
    nan_mask = np.isnan(numeric)
    multiplier = 10**scale
    scaled = numeric * float(multiplier)
    if np.any(~np.isfinite(scaled) & ~nan_mask):
        fail(
            "E_FIXED_OVERFLOW",
            f"fixed-point scaling overflowed before rounding for {field}",
            node_id=node_id,
            details={"scale": scale, "multiplier": multiplier, "rounding": ROUNDING_MODE},
        )
    scaled = np.where(nan_mask, 0.0, scaled)
    rounded = np.trunc(scaled + np.copysign(0.5, scaled))
    if np.any(rounded > float(INT64_MAX)) or np.any(rounded < float(INT64_MIN)):
        fail(
            "E_FIXED_OVERFLOW",
            f"fixed-point conversion overflowed int64 for {field}",
            node_id=node_id,
            details={"scale": scale, "multiplier": multiplier, "rounding": ROUNDING_MODE},
        )
    return rounded.astype(np.int64)


def fixed_sum_exact(values: np.ndarray, scale: int, *, node_id: str, field: str) -> int:
    converted = fixed_convert(values, scale, node_id=node_id, field=field)
    result = sum(int(value) for value in converted.tolist())
    if result < INT64_MIN or result > INT64_MAX:
        fail(
            "E_FIXED_OVERFLOW",
            f"fixed-point aggregate overflowed int64 for {field}",
            node_id=node_id,
            details={"scale": scale, "multiplier": 10**scale, "rounding": ROUNDING_MODE},
        )
    return result


def _numeric_nonnan(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    array = np.asarray(values)
    if array.dtype.kind == "f":
        mask = ~np.isnan(array)
    else:
        mask = np.ones(array.shape, dtype=bool)
    return array.astype(np.float64, copy=False), mask


def _groups(relation: Relation, columns: list[str]) -> tuple[list[tuple[Any, ...]], np.ndarray]:
    if not columns:
        return [tuple()], np.zeros(relation.row_count, dtype=np.int64)
    dtype = [(f"f{index}", relation.columns[name].dtype) for index, name in enumerate(columns)]
    structured = np.empty(relation.row_count, dtype=dtype)
    for index, name in enumerate(columns):
        structured[f"f{index}"] = relation.columns[name]
    unique, inverse = np.unique(structured, return_inverse=True)
    groups = [tuple(_json_scalar(value) for value in row) for row in unique.tolist()]
    return groups, inverse.astype(np.int64, copy=False)


def new_window_state(node: dict[str, Any]) -> dict[str, Any]:
    source_columns = sorted(
        {function["column"] for function in node["functions"]}
        | set(node.get("partition_by", []))
        | {node["order_by"]}
    )
    return {
        "kind": "window",
        "node_id": node["id"],
        "length": node["length"],
        "step": node["step"],
        "partition_by": list(node.get("partition_by", [])),
        "order_by": node["order_by"],
        "source_columns": source_columns,
        "partitions": {},
    }


def _history_arrays(
    partition_state: dict[str, Any] | None,
    source_columns: list[str],
    dtypes: dict[str, str],
) -> dict[str, np.ndarray]:
    if partition_state is None:
        return {
            name: np.asarray([], dtype=_numpy_dtype(dtypes[name])) for name in source_columns
        }
    history = partition_state.get("history", {})
    return {
        name: np.asarray(history.get(name, []), dtype=_numpy_dtype(dtypes[name]))
        for name in source_columns
    }


def apply_window(
    state: dict[str, Any],
    relation: Relation,
    node: dict[str, Any],
    config: ExecutionConfig,
    *,
    shard_id: int,
) -> tuple[dict[str, Any], Relation]:
    node_id = node["id"]
    length = int(node["length"])
    step = int(node["step"])
    partition_by = list(node.get("partition_by", []))
    order_by = node["order_by"]
    source_columns = state["source_columns"]

    estimate = relation_bytes(relation)
    for partition_state in state["partitions"].values():
        for values in partition_state.get("history", {}).values():
            estimate += len(values) * 8
    if estimate > config.operator_buffer_budget_mib * 1024 * 1024:
        fail(
            "E_BUFFER_BUDGET",
            "window operator buffer exceeds configured budget",
            node_id=node_id,
            shard_id=shard_id,
            details={
                "estimated_bytes": estimate,
                "budget_bytes": int(config.operator_buffer_budget_mib * 1024 * 1024),
            },
        )

    groups, inverse = _groups(relation, partition_by)
    output_parts: list[Relation] = []
    updated = json.loads(json.dumps(state))
    updated["partitions"] = dict(state["partitions"])
    dtypes = {name: dtype_name(relation.columns[name]) for name in source_columns}
    keep = max(length - 1, 0)

    for group_index, group_values in enumerate(groups):
        key = json_key(list(group_values))
        group_mask = inverse == group_index
        indexes = np.flatnonzero(group_mask)
        if indexes.size == 0:
            continue
        order_values = relation.columns[order_by][indexes]
        stable_order = np.argsort(order_values, kind="stable")
        indexes = indexes[stable_order]
        partition_state = state["partitions"].get(key)
        previous_count = int(partition_state["count"]) if partition_state else 0
        history = _history_arrays(partition_state, source_columns, dtypes)
        history_count = int(history[order_by].size)
        current_order = relation.columns[order_by][indexes]
        if history_count and current_order.size:
            previous_order = history[order_by][-1]
            if current_order[0] < previous_order:
                fail(
                    "E_WINDOW_ORDER",
                    "window order key regressed across committed shards",
                    node_id=node_id,
                    shard_id=shard_id,
                    details={
                        "partition": list(group_values),
                        "previous_order": _json_scalar(previous_order),
                        "current_order": _json_scalar(current_order[0]),
                    },
                )
        combined = {
            name: np.concatenate([history[name], relation.columns[name][indexes]])
            for name in source_columns
        }
        combined_bounds = {
            name: np.concatenate(
                [
                    np.zeros(history[name].size, dtype=np.float64),
                    relation.error_bounds.get(
                        name, np.zeros(indexes.size, dtype=np.float64)
                    )[indexes],
                ]
            )
            for name in source_columns
        }
        total = previous_count + indexes.size
        base_index = previous_count - history_count
        first_k = max(0, math.ceil((previous_count - length + 1) / step))
        last_k = math.floor((total - length) / step)
        starts = (
            np.arange(first_k, last_k + 1, dtype=np.int64) * step
            if last_k >= first_k
            else np.asarray([], dtype=np.int64)
        )
        starts = starts[starts >= base_index]
        if starts.size and (
            int(starts.max()) - base_index + length - 1 >= combined[order_by].size
        ):
            fail(
                "E_STATE",
                "window slice computation exceeded the retained history buffer",
                node_id=node_id,
                shard_id=shard_id,
                details={"starts": starts.tolist(), "base": base_index},
            )

        if starts.size:
            absolute_indexes = starts - base_index
            output_columns: dict[str, np.ndarray] = {}
            output_bounds: dict[str, np.ndarray] = {}
            output_sources: dict[str, list[str]] = {}
            repeated = len(starts)
            for name in partition_by:
                output_columns[name] = np.full(
                    repeated, group_values[partition_by.index(name)], dtype=relation.columns[name].dtype
                )
            output_columns["_window_start"] = starts.astype(np.int64)
            output_columns["_window_end"] = (starts + length).astype(np.int64)
            output_columns["_window_order_end"] = combined[order_by][
                absolute_indexes + length - 1
            ]
            output_sources["_window_start"] = [f"{node_id}:window_start"]
            output_sources["_window_end"] = [f"{node_id}:window_end"]
            output_sources["_window_order_end"] = [
                f"scan:{order_by}",
                f"{node_id}:window_order_end",
            ]
            output_bounds["_window_start"] = np.zeros(repeated, dtype=np.float64)
            output_bounds["_window_end"] = np.zeros(repeated, dtype=np.float64)
            output_bounds["_window_order_end"] = np.zeros(repeated, dtype=np.float64)
            for name in partition_by:
                output_bounds[name] = np.zeros(repeated, dtype=np.float64)
                output_sources[name] = [f"scan:{name}", "no_numeric_error"]

            for function in node["functions"]:
                name = function["name"]
                op = function["op"]
                source = function["column"]
                values = np.asarray(combined[source])
                windows = sliding_window_view(values, length)[absolute_indexes]
                input_bounds = sliding_window_view(combined_bounds[source], length)[absolute_indexes]
                result, bound = _window_reduce(
                    windows,
                    input_bounds,
                    op,
                    scale=function.get("scale", config.fixed_scale),
                    node_id=node_id,
                    field=name,
                )
                output_columns[name] = result
                output_bounds[name] = bound
                output_sources[name] = (
                    list(relation.error_sources.get(source, [f"scan:{source}"]))
                    + [f"{node_id}:window len={length} step={step}"]
                    + ([f"{node_id}:{ROUNDING_MODE} scale={function.get('scale', config.fixed_scale)}"] if op == "fixed_sum" else [])
                    + [f"{node_id}:{SUM_ORDER}"]
                )
            output_parts.append(
                Relation(
                    columns=output_columns,
                    row_count=repeated,
                    global_start=relation.global_start,
                    error_bounds=output_bounds,
                    error_sources=output_sources,
                    source_shards=[shard_id],
                )
            )

        partition_state = dict(
            partition_state
            or {
                "partition": list(group_values),
                "count": 0,
                "cumulative": {},
                "history": {},
            }
        )
        cumulative = dict(partition_state.get("cumulative", {}))
        for name in source_columns:
            source_values = relation.columns[name][indexes]
            numeric, mask = _numeric_nonnan(source_values)
            accumulator = cumulative.setdefault(
                name,
                {"count": 0, "sum": 0.0, "sumsq": 0.0, "abs_sum": 0.0, "nan_count": 0},
            )
            accumulator["count"] = int(accumulator["count"]) + int(np.count_nonzero(mask))
            accumulator["sum"] = float(accumulator["sum"]) + float(
                np.sum(numeric[mask], dtype=np.float64)
            )
            accumulator["sumsq"] = float(accumulator["sumsq"]) + float(
                np.sum(numeric[mask] * numeric[mask], dtype=np.float64)
            )
            accumulator["abs_sum"] = float(accumulator["abs_sum"]) + float(
                np.sum(np.abs(numeric[mask]), dtype=np.float64)
            )
            accumulator["nan_count"] = int(accumulator["nan_count"]) + int(
                source_values.size - np.count_nonzero(mask)
            )
        partition_state["count"] = total
        partition_state["cumulative"] = cumulative
        keep_rows = combined[order_by][-keep:] if keep else np.asarray([], dtype=combined[order_by].dtype)
        partition_state["history"] = {
            name: [_json_scalar(value) for value in combined[name][-keep:].tolist()]
            if keep
            else []
            for name in source_columns
        }
        partition_state["last_order"] = _json_scalar(keep_rows[-1]) if keep_rows.size else None
        updated["partitions"][key] = partition_state

    if not output_parts:
        schema = {
            name: dtype_name(relation.columns[name]) for name in partition_by
        }
        schema.update(
            {
                "_window_start": "int64",
                "_window_end": "int64",
                "_window_order_end": dtype_name(relation.columns[order_by]),
            }
        )
        for function in node["functions"]:
            schema[function["name"]] = (
                "int64" if function["op"] in {"count", "fixed_sum"} else "float64"
            )
        output = empty_relation(schema)
    else:
        output = _concat_relations(output_parts)
    if len(canonical_json(updated)) > config.operator_buffer_budget_mib * 1024 * 1024:
        fail(
            "E_BUFFER_BUDGET",
            "persisted window state exceeds configured operator buffer budget",
            node_id=node_id,
            shard_id=shard_id,
        )
    return updated, output


def _window_reduce(
    windows: np.ndarray,
    input_bounds: np.ndarray,
    op: str,
    *,
    scale: int,
    node_id: str,
    field: str,
) -> tuple[np.ndarray, np.ndarray]:
    numeric, mask = _numeric_nonnan(windows)
    length = windows.shape[1]
    nonnan_count = np.sum(mask, axis=1)
    bound = np.sum(input_bounds, axis=1)
    if op == "count":
        return nonnan_count.astype(np.int64), np.zeros(windows.shape[0], dtype=np.float64)
    if op == "sum":
        return (
            np.sum(np.where(mask, numeric, 0.0), axis=1, dtype=np.float64),
            bound
            + EPS
            * max(1, length)
            * np.sum(np.where(mask, np.abs(numeric), 0.0), axis=1, dtype=np.float64),
        )
    if op == "sumsq":
        squares = np.where(mask, numeric * numeric, 0.0)
        return (
            np.sum(squares, axis=1, dtype=np.float64),
            bound
            + EPS
            * max(1, length)
            * np.sum(np.abs(squares), axis=1, dtype=np.float64),
        )
    if op == "avg":
        total = np.sum(np.where(mask, numeric, 0.0), axis=1, dtype=np.float64)
        result = np.divide(
            total,
            nonnan_count,
            out=np.full_like(total, np.nan),
            where=nonnan_count > 0,
        )
        return (
            result,
            bound / np.maximum(nonnan_count, 1)
            + EPS
            * max(1, length)
            * np.abs(result),
        )
    if op in {"min", "max"}:
        fill = np.inf if op == "min" else -np.inf
        filled = np.where(mask, numeric, fill)
        reducer = np.min if op == "min" else np.max
        result = reducer(filled, axis=1)
        result = np.where(nonnan_count > 0, result, np.nan)
        return result.astype(np.float64), np.max(input_bounds, axis=1)
    if op == "fixed_sum":
        converted = fixed_convert(windows, scale, node_id=node_id, field=field)
        object_sums = np.sum(converted.astype(object), axis=1)
        values = np.asarray([int(value) for value in object_sums], dtype=object)
        overflow = np.asarray(
            [(value < INT64_MIN or value > INT64_MAX) for value in values], dtype=bool
        )
        if np.any(overflow):
            fail(
                "E_FIXED_OVERFLOW",
                f"fixed-point window aggregate overflowed int64 for {field}",
                node_id=node_id,
                details={"scale": scale, "rounding": ROUNDING_MODE},
            )
        return values.astype(np.int64), np.zeros(windows.shape[0], dtype=np.float64)
    raise AssertionError(op)


def _concat_relations(parts: list[Relation]) -> Relation:
    if not parts:
        return Relation(columns={}, row_count=0)
    names = list(parts[0].columns)
    output = {
        name: np.concatenate([part.columns[name] for part in parts])
        for name in names
    }
    bounds = {
        name: np.concatenate(
            [
                part.error_bounds.get(name, np.zeros(part.row_count, dtype=np.float64))
                for part in parts
            ]
        )
        for name in names
    }
    sources: dict[str, list[str]] = {}
    for name in names:
        for part in parts:
            sources.setdefault(name, [])
            for source in part.error_sources.get(name, []):
                if source not in sources[name]:
                    sources[name].append(source)
    shards: list[int] = []
    for part in parts:
        for shard in part.source_shards:
            if shard not in shards:
                shards.append(shard)
    return Relation(
        columns=output,
        row_count=sum(part.row_count for part in parts),
        error_bounds=bounds,
        error_sources=sources,
        source_shards=shards,
    )


def new_aggregate_state(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "aggregate",
        "node_id": node["id"],
        "group_by": list(node.get("group_by", [])),
        "groups": {},
        "seen_rows": 0,
    }


def apply_aggregate(
    state: dict[str, Any],
    relation: Relation,
    node: dict[str, Any],
    config: ExecutionConfig,
    *,
    shard_id: int,
) -> dict[str, Any]:
    node_id = node["id"]
    groups, inverse = _groups(relation, node["group_by"])
    updated = dict(state)
    updated["groups"] = dict(state["groups"])
    updated["seen_rows"] = int(state["seen_rows"]) + relation.row_count

    for group_index, group_values in enumerate(groups):
        mask = inverse == group_index
        if not np.any(mask):
            continue
        key = json_key(list(group_values))
        group_state = dict(
            updated["groups"].get(
                key,
                {"group": list(group_values), "aggregates": {}},
            )
        )
        accumulators = dict(group_state["aggregates"])
        for aggregate in node["aggregates"]:
            name = aggregate["name"]
            source = aggregate["column"]
            op = aggregate["op"]
            values = relation.columns[source][mask]
            input_bounds = relation.error_bounds.get(
                source, np.zeros(values.size, dtype=np.float64)
            )[mask]
            partial = _aggregate_partial(
                values,
                input_bounds,
                op,
                scale=aggregate.get("scale", config.fixed_scale),
                node_id=node_id,
                field=name,
            )
            accumulator = accumulators.get(name)
            if accumulator is None:
                accumulator = {
                    "op": op,
                    "source": source,
                    "count": 0,
                    "sum": 0.0,
                    "sumsq": 0.0,
                    "abs_sum": 0.0,
                    "input_error": 0.0,
                    "nan_count": 0,
                    "min": None,
                    "max": None,
                    "fixed_sum": 0,
                    "dtype": dtype_name(values),
                    "scale": aggregate.get("scale", config.fixed_scale)
                    if op == "fixed_sum"
                    else None,
                    "sources": list(
                        relation.error_sources.get(source, [f"scan:{source}"])
                    )
                    + [f"{node_id}:aggregate {op}"],
                }
            else:
                accumulator["sources"] = list(
                    dict.fromkeys(
                        list(accumulator.get("sources", []))
                        + list(relation.error_sources.get(source, [f"scan:{source}"]))
                        + [f"{node_id}:aggregate {op}"]
                    )
                )
            accumulator["count"] = int(accumulator["count"]) + partial["count"]
            accumulator["sum"] = float(accumulator["sum"]) + partial["sum"]
            accumulator["sumsq"] = float(accumulator["sumsq"]) + partial["sumsq"]
            accumulator["abs_sum"] = float(accumulator["abs_sum"]) + partial["abs_sum"]
            accumulator["input_error"] = float(accumulator["input_error"]) + partial["input_error"]
            accumulator["nan_count"] = int(accumulator["nan_count"]) + partial["nan_count"]
            if partial["min"] is not None:
                previous = accumulator["min"]
                accumulator["min"] = (
                    partial["min"] if previous is None else min(previous, partial["min"])
                )
            if partial["max"] is not None:
                previous = accumulator["max"]
                accumulator["max"] = (
                    partial["max"] if previous is None else max(previous, partial["max"])
                )
            if op == "fixed_sum":
                total = int(accumulator["fixed_sum"]) + int(partial["fixed_sum"])
                if total < INT64_MIN or total > INT64_MAX:
                    fail(
                        "E_FIXED_OVERFLOW",
                        f"fixed-point aggregate overflowed int64 for {name}",
                        node_id=node_id,
                        shard_id=shard_id,
                        details={"scale": accumulator["scale"], "rounding": ROUNDING_MODE},
                    )
                accumulator["fixed_sum"] = total
            accumulators[name] = accumulator
        group_state["aggregates"] = accumulators
        updated["groups"][key] = group_state
    if len(canonical_json(updated)) > config.operator_buffer_budget_mib * 1024 * 1024:
        fail(
            "E_BUFFER_BUDGET",
            "aggregate state exceeds configured operator buffer budget",
            node_id=node_id,
            shard_id=shard_id,
        )
    return updated


def _aggregate_partial(
    values: np.ndarray,
    input_bounds: np.ndarray,
    op: str,
    *,
    scale: int,
    node_id: str,
    field: str,
) -> dict[str, Any]:
    numeric, mask = _numeric_nonnan(values)
    count = int(np.count_nonzero(mask))
    nan_count = int(values.size - count)
    if op == "count":
        return {
            "count": count,
            "sum": 0.0,
            "sumsq": 0.0,
            "abs_sum": 0.0,
            "input_error": 0.0,
            "nan_count": nan_count,
            "min": None,
            "max": None,
            "fixed_sum": 0,
        }
    if op == "fixed_sum":
        return {
            "count": count,
            "sum": 0.0,
            "sumsq": 0.0,
            "abs_sum": 0.0,
            "input_error": 0.0,
            "nan_count": nan_count,
            "min": None,
            "max": None,
            "fixed_sum": fixed_sum_exact(values, scale, node_id=node_id, field=field),
        }
    selected = numeric[mask]
    total = float(np.sum(selected, dtype=np.float64))
    squares = float(np.sum(selected * selected, dtype=np.float64))
    abs_sum = float(np.sum(np.abs(selected), dtype=np.float64))
    minimum = _json_scalar(np.min(selected)) if count else None
    maximum = _json_scalar(np.max(selected)) if count else None
    return {
        "count": count,
        "sum": total,
        "sumsq": squares,
        "abs_sum": abs_sum,
        "input_error": float(np.sum(input_bounds, dtype=np.float64)),
        "nan_count": nan_count,
        "min": minimum if op == "min" else None,
        "max": maximum if op == "max" else None,
        "fixed_sum": 0,
    }


def finalize_aggregate(
    state: dict[str, Any],
    node: dict[str, Any],
    schema: dict[str, str],
) -> Relation:
    groups = state["groups"]
    if not groups and not node.get("group_by"):
        groups = {
            "[]": {
                "group": [],
                "aggregates": {
                    aggregate["name"]: {
                        "op": aggregate["op"],
                        "source": aggregate["column"],
                        "count": 0,
                        "sum": 0.0,
                        "sumsq": 0.0,
                        "abs_sum": 0.0,
                        "input_error": 0.0,
                        "nan_count": 0,
                        "min": None,
                        "max": None,
                        "fixed_sum": 0,
                        "dtype": "float64",
                        "scale": aggregate.get("scale"),
                        "sources": [f"scan:{aggregate['column']}"],
                    }
                    for aggregate in node["aggregates"]
                },
            }
        }
    ordered = sorted(
        groups.values(),
        key=lambda group_state: tuple(group_state["group"]),
    )
    output: dict[str, np.ndarray] = {
        name: np.asarray(
            [group_state["group"][index] for group_state in ordered],
            dtype=_numpy_dtype(schema[name]),
        )
        for index, name in enumerate(node.get("group_by", []))
    }
    bounds: dict[str, np.ndarray] = {
        name: np.zeros(len(ordered), dtype=np.float64) for name in node.get("group_by", [])
    }
    sources: dict[str, list[str]] = {
        name: [f"scan:{name}", "no_numeric_error"] for name in node.get("group_by", [])
    }
    for aggregate in node["aggregates"]:
        name = aggregate["name"]
        op = aggregate["op"]
        values: list[Any] = []
        error_values: list[float] = []
        for group_state in ordered:
            accumulator = group_state["aggregates"].get(name)
            if accumulator is None:
                value = 0 if op in {"count", "fixed_sum"} else math.nan
                bound = 0.0
            elif op == "count":
                value = int(accumulator["count"])
                bound = 0.0
            elif op == "sum":
                value = float(accumulator["sum"])
                bound = float(accumulator["input_error"]) + EPS * max(
                    1, int(accumulator["count"])
                ) * float(accumulator["abs_sum"])
            elif op == "sumsq":
                value = float(accumulator["sumsq"])
                bound = float(accumulator["input_error"]) + EPS * max(
                    1, int(accumulator["count"])
                ) * float(accumulator["sumsq"])
            elif op == "avg":
                count = int(accumulator["count"])
                value = float(accumulator["sum"]) / count if count else math.nan
                bound = (
                    float(accumulator["input_error"]) / max(1, count)
                    + EPS
                    * max(1, count)
                    * float(accumulator["abs_sum"])
                    / max(1, count)
                    + EPS * abs(value)
                )
            elif op == "min":
                value = accumulator["min"] if accumulator["min"] is not None else math.nan
                bound = 0.0
            elif op == "max":
                value = accumulator["max"] if accumulator["max"] is not None else math.nan
                bound = 0.0
            elif op == "fixed_sum":
                value = int(accumulator["fixed_sum"])
                bound = 0.0
            else:
                raise AssertionError(op)
            values.append(value)
            error_values.append(bound)
        dtype = _numpy_dtype(schema[name])
        if op == "avg" and dtype != np.float64:
            dtype = np.dtype(np.float64)
        output[name] = np.asarray(values, dtype=dtype)
        bounds[name] = np.asarray(error_values, dtype=np.float64)
        source_chain: list[str] = []
        for group_state in ordered:
            accumulator = group_state["aggregates"].get(name)
            if accumulator:
                source_chain = list(accumulator.get("sources", []))
                break
        if not source_chain:
            source_chain = [f"scan:{aggregate['column']}"]
        sources[name] = list(
            dict.fromkeys(
                source_chain
                + [
                    f"{node['id']}:{SUM_ORDER}",
                    f"{node['id']}:{NAN_POLICY}",
                ]
            )
        )
        if op == "fixed_sum":
            sources[name].append(
                f"{node['id']}:{ROUNDING_MODE} scale={aggregate.get('scale')}"
            )
    return Relation(
        columns=output,
        row_count=len(ordered),
        error_bounds=bounds,
        error_sources=sources,
    )


def state_watermark(state: dict[str, Any] | None) -> int:
    if not state:
        return -1
    return int(state.get("watermark", -1))


def attach_watermark(state: dict[str, Any], shard_id: int) -> dict[str, Any]:
    result = dict(state)
    result["watermark"] = shard_id
    return result


def run_node_chain(
    plan: Any,
    start_relation: Relation,
    starting_node_id: str,
    node_states: dict[str, dict[str, Any]],
    config: ExecutionConfig,
    *,
    shard_id: int,
    finalize: bool,
) -> tuple[dict[str, dict[str, Any]], Relation | None, dict[str, Relation]]:
    """Apply the chain after a materialized boundary.

    Returns updated stateful node states, a root relation if available for this shard,
    and finalized outputs for stateful nodes.
    """
    relation: Relation | None = start_relation
    states = {name: json.loads(json.dumps(value)) for name, value in node_states.items()}
    node_outputs: dict[str, Relation] = {}
    cursor: str | None = starting_node_id
    blocked = False

    while cursor is not None:
        node = plan.nodes[cursor]
        node_type = node["type"]
        if node_type in {"scan", "filter", "project"}:
            if relation is not None:
                if node_type == "filter":
                    relation = apply_filter(relation, node["predicate"])
                elif node_type == "project":
                    relation = apply_project(relation, node["columns"])
                node_outputs[cursor] = relation
        elif node_type == "window":
            if relation is not None:
                state = states.get(cursor) or new_window_state(node)
                state, relation = apply_window(
                    state, relation, node, config, shard_id=shard_id
                )
                states[cursor] = attach_watermark(state, shard_id)
                node_outputs[cursor] = relation
        elif node_type == "aggregate":
            if relation is not None:
                state = states.get(cursor) or new_aggregate_state(node)
                state = apply_aggregate(
                    state, relation, node, config, shard_id=shard_id
                )
                states[cursor] = attach_watermark(state, shard_id)
            blocked = True
            relation = None
        if cursor == plan.root:
            break
        parents = [
            parent_id
            for parent_id, candidate in plan.nodes.items()
            if candidate.get("input") == cursor
        ]
        cursor = parents[0] if parents else None

    if blocked and finalize:
        relation = _finalize_tail(
            plan,
            states,
            config,
            node_outputs,
        )
    return states, relation, node_outputs


def _finalize_tail(
    plan: Any,
    states: dict[str, dict[str, Any]],
    config: ExecutionConfig,
    node_outputs: dict[str, Relation],
) -> Relation | None:
    relation: Relation | None = None
    seen_blocking = False
    for node_id in plan.chain:
        node = plan.nodes[node_id]
        if node["type"] in {"scan", "filter"}:
            continue
        if node["type"] == "project":
            if seen_blocking and relation is not None:
                relation = apply_project(relation, node["columns"])
                node_outputs[node_id] = relation
            continue
        if node["type"] == "window":
            if seen_blocking and relation is not None:
                state = new_window_state(node)
                state, relation = apply_window(
                    state, relation, node, config, shard_id=-1
                )
                state = attach_watermark(state, -1)
                states[node_id] = state
                node_outputs[node_id] = relation
            continue
        if node["type"] == "aggregate":
            if relation is None and not seen_blocking:
                state = states[node_id]
                relation = finalize_aggregate(state, node, plan.schemas[node_id])
            elif seen_blocking and relation is not None:
                state = new_aggregate_state(node)
                state = apply_aggregate(
                    state, relation, node, config, shard_id=-1
                )
                state = attach_watermark(state, -1)
                states[node_id] = state
                relation = finalize_aggregate(state, node, plan.schemas[node_id])
            seen_blocking = True
            node_outputs[node_id] = relation
    return relation

