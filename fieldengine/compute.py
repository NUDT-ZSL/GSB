from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Iterable

import numpy as np

from .errors import EngineError, fail
from .exact import (
    add_bins,
    bins_from_pairs,
    bins_to_fraction,
    bins_to_pairs,
    exact_reduce,
    fixed_variance_fraction,
    float_bits,
    fraction_to_float,
    json_number,
    mean_float_from_bins,
    sum_float_from_bins,
    variance_float_from_bins,
)
from .model import AggregateSpec, Batch, Column, FilterSpec, Query, canonical_json


INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


def apply_filter(batch: Batch, filters: tuple[FilterSpec, ...], start: int, stop: int) -> np.ndarray:
    n = stop - start
    mask = np.ones(n, dtype=np.bool_)
    for filt in filters:
        values = batch.columns[filt.column].values[start:stop]
        typ = batch.columns[filt.column].type
        op = filt.op
        raw = filt.value
        if op == "is_nan":
            part = np.isnan(values)
        elif op == "is_finite":
            part = np.isfinite(values)
        elif op == "in":
            try:
                if typ == "float":
                    candidates = np.asarray(raw, dtype=np.float64)
                elif typ == "bool":
                    if any(not isinstance(x, bool) for x in raw):
                        raise TypeError("bool column requires boolean candidates")
                    candidates = np.asarray(raw, dtype=np.bool_)
                else:
                    if any(isinstance(x, bool) or not isinstance(x, int) for x in raw):
                        raise TypeError("int column requires integer candidates")
                    candidates = np.asarray(raw, dtype=np.int64)
            except (TypeError, ValueError, OverflowError) as exc:
                raise fail(
                    "E_QUERY_FILTER",
                    "filter value is not comparable with the column type",
                    column=filt.column,
                    type=typ,
                    reason=str(exc),
                ) from exc
            part = np.isin(values, candidates)
        else:
            try:
                if typ == "float":
                    rhs = np.float64(raw)
                elif typ == "bool":
                    if not isinstance(raw, bool):
                        raise TypeError("bool column requires a boolean literal")
                    rhs = np.bool_(raw)
                else:
                    if isinstance(raw, bool) or not isinstance(raw, int):
                        raise TypeError("int column requires an integer literal")
                    rhs = np.int64(raw)
            except (TypeError, ValueError, OverflowError) as exc:
                raise fail(
                    "E_QUERY_FILTER",
                    "filter value is not comparable with the column type",
                    column=filt.column,
                    type=typ,
                    reason=str(exc),
                ) from exc
            with np.errstate(invalid="ignore"):
                if op == "eq":
                    part = values == rhs
                elif op == "ne":
                    part = values != rhs
                elif op == "lt":
                    part = values < rhs
                elif op == "le":
                    part = values <= rhs
                elif op == "gt":
                    part = values > rhs
                elif op == "ge":
                    part = values >= rhs
                else:
                    raise fail("E_QUERY_FILTER", "unsupported filter operation", op=op)
        mask &= part
    return np.asarray(mask, dtype=np.bool_)


def _key_bits(column: Column, values: np.ndarray) -> np.ndarray:
    if column.type == "float":
        bits = np.ascontiguousarray(values, dtype=np.float64).view(np.uint64).copy()
        nan_bits = np.uint64(0x7FF8000000000000)
        bits[np.isnan(values)] = nan_bits
        bits[values == 0.0] = np.uint64(0)
        return bits
    if column.type == "bool":
        return np.ascontiguousarray(values, dtype=np.uint8)
    return np.ascontiguousarray(values, dtype=np.int64).view(np.uint64)


def _decode_key_value(column: Column, value: Any) -> Any:
    if column.type == "bool":
        return bool(value)
    if column.type == "int":
        return int(value)
    numeric = float(value)
    return json_number(numeric)


def group_rows(
    batch: Batch,
    columns: tuple[str, ...],
    start: int,
    stop: int,
    selected: np.ndarray | None = None,
) -> tuple[np.ndarray, list[str], list[dict[str, Any]], int]:
    """Return group ids, canonical key strings, display keys, and group count."""
    if selected is None:
        base_len = stop - start
        selected = np.arange(base_len, dtype=np.int64)
    selected = np.asarray(selected, dtype=np.int64)
    n = int(selected.size)
    key_columns = [batch.columns[name] for name in columns]
    if not columns:
        keys = [canonical_json({})] if n else []
        displays = [{}] if n else []
        return np.zeros(n, dtype=np.int64), keys, displays, (1 if n else 0)
    if n == 0:
        return np.empty(0, dtype=np.int64), [], [], 0
    arrays = [col.values[start:stop] for col in key_columns]
    bits_arrays = [
        _key_bits(col, arr[selected]) for col, arr in zip(key_columns, arrays, strict=True)
    ]
    order = np.lexsort(tuple(reversed(bits_arrays)))
    change = np.zeros(n, dtype=np.bool_)
    change[0] = True
    for bits in bits_arrays:
        sorted_bits = bits[order]
        change[1:] |= sorted_bits[1:] != sorted_bits[:-1]
    sorted_ids = np.cumsum(change) - 1
    ids = np.empty(n, dtype=np.int64)
    ids[order] = sorted_ids
    first_rows = order[np.flatnonzero(change)]
    displays: list[dict[str, Any]] = []
    keys: list[str] = []
    for row_index in first_rows.tolist():
        display = {
            col.name: _decode_key_value(col, arr[selected[row_index]])
            for col, arr in zip(key_columns, arrays, strict=True)
        }
        displays.append(display)
        keys.append(canonical_json(display))
    return ids, keys, displays, len(keys)


def partition_rows(
    batch: Batch,
    columns: tuple[str, ...],
    start: int,
    stop: int,
    selected: np.ndarray,
) -> tuple[np.ndarray, list[str], list[dict[str, Any]], int]:
    if columns:
        return group_rows(batch, columns, start, stop, selected)
    n = int(selected.size)
    if n == 0:
        return np.empty(0, dtype=np.int64), [], [], 0
    return np.zeros(n, dtype=np.int64), [canonical_json({})], [{}], 1


def quantize_fixed(column: Column, values: np.ndarray, scale: int) -> tuple[np.ndarray, np.ndarray]:
    factor = 10**scale
    overflow = np.zeros(values.shape, dtype=np.bool_)
    if column.type == "float":
        with np.errstate(over="ignore", invalid="ignore"):
            scaled = values.astype(np.float64) * factor
        range_ok = np.isfinite(scaled) & (scaled >= float(INT64_MIN)) & (scaled < float(2**63))
        overflow = np.isfinite(values) & ~range_ok
        units = np.zeros(values.shape, dtype=np.int64)
        if np.any(range_ok):
            units[range_ok] = np.rint(scaled[range_ok]).astype(np.int64)
        return units, overflow
    if column.type == "bool":
        return values.astype(np.int64) * factor, overflow
    limit = INT64_MAX // factor
    overflow = (values > limit) | (values < -limit)
    units = np.zeros(values.shape, dtype=np.int64)
    safe = ~overflow
    units[safe] = values[safe].astype(np.int64) * factor
    return units, overflow


def make_metric_state(
    values: np.ndarray,
    column: Column,
    group_ids: np.ndarray,
    n_groups: int,
    scale: int,
) -> dict[str, Any]:
    exact = exact_reduce(values.astype(np.float64), group_ids, n_groups)
    units, overflow = quantize_fixed(column, values, scale)
    fixed_sum: list[int] = []
    fixed_square_sum: list[int] = []
    for group in range(n_groups):
        mask = group_ids == group
        if np.any(mask):
            group_units = units[mask].astype(object)
            fixed_sum.append(int(np.sum(group_units, dtype=object)))
            fixed_square_sum.append(int(np.sum(group_units * group_units, dtype=object)))
        else:
            fixed_sum.append(0)
            fixed_square_sum.append(0)
    overflow_counts = np.bincount(group_ids[overflow], minlength=n_groups).astype(np.int64).tolist()
    fixed_bad = [
        int(exact["anomalies"]["nan"][i])
        + int(exact["anomalies"]["posinf"][i])
        + int(exact["anomalies"]["neginf"][i])
        + int(overflow_counts[i])
        for i in range(n_groups)
    ]
    partial_mean_bits = []
    for i in range(n_groups):
        anomaly_i = {
            "nan": int(exact["anomalies"]["nan"][i]),
            "posinf": int(exact["anomalies"]["posinf"][i]),
            "neginf": int(exact["anomalies"]["neginf"][i]),
        }
        partial_mean_bits.append(
            float_bits(mean_float_from_bins(exact["sum_bins"][i], anomaly_i, int(exact["counts"][i])))
        )
    return {
        "counts": exact["counts"],
        "sum_bins": [bins_to_pairs(x) for x in exact["sum_bins"]],
        "square_bins": [bins_to_pairs(x) for x in exact["square_bins"]],
        "anomalies": {
            **exact["anomalies"],
            "fixed_overflow": overflow_counts,
        },
        "max_abs": exact["max_abs"],
        "fixed": {
            "sum_units": fixed_sum,
            "square_sum_units": fixed_square_sum,
            "bad_counts": fixed_bad,
        },
        "column_type": column.type,
        "partial_mean_bits": partial_mean_bits,
        "aggregation_order": "canonical_shard_order_then_exact_bin_merge",
    }


def merge_metric_states(states: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    result: dict[str, Any] | None = None
    for state in states:
        if result is None:
            result = {
                "counts": [int(x) for x in state["counts"]],
                "sum_bins": [bins_from_pairs(x) for x in state["sum_bins"]],
                "square_bins": [bins_from_pairs(x) for x in state["square_bins"]],
                "anomalies": {
                    "nan": [int(x) for x in state["anomalies"]["nan"]],
                    "posinf": [int(x) for x in state["anomalies"]["posinf"]],
                    "neginf": [int(x) for x in state["anomalies"]["neginf"]],
                    "fixed_overflow": [int(x) for x in state["anomalies"]["fixed_overflow"]],
                },
                "max_abs": [float(x) for x in state["max_abs"]],
                "fixed": {
                    "sum_units": [int(x) for x in state["fixed"]["sum_units"]],
                    "square_sum_units": [int(x) for x in state["fixed"]["square_sum_units"]],
                    "bad_counts": [int(x) for x in state["fixed"]["bad_counts"]],
                },
                "column_type": state["column_type"],
            }
            continue
        for i, count in enumerate(state["counts"]):
            result["counts"][i] += int(count)
            add_bins(result["sum_bins"][i], bins_from_pairs(state["sum_bins"][i]))
            add_bins(result["square_bins"][i], bins_from_pairs(state["square_bins"][i]))
            for anomaly in ("nan", "posinf", "neginf", "fixed_overflow"):
                result["anomalies"][anomaly][i] += int(state["anomalies"][anomaly][i])
            result["max_abs"][i] = max(result["max_abs"][i], float(state["max_abs"][i]))
            result["fixed"]["sum_units"][i] += int(state["fixed"]["sum_units"][i])
            result["fixed"]["square_sum_units"][i] += int(
                state["fixed"]["square_sum_units"][i]
            )
            result["fixed"]["bad_counts"][i] += int(state["fixed"]["bad_counts"][i])
    return result


def _anomaly_slice(state: dict[str, Any], index: int) -> dict[str, int]:
    return {name: int(values[index]) for name, values in state["anomalies"].items()}


def finalize_metric(
    state: dict[str, Any],
    spec: AggregateSpec,
    index: int,
    count: int,
    scale: int,
) -> dict[str, Any]:
    sum_bins = state["sum_bins"][index]
    square_bins = state["square_bins"][index]
    anomaly = _anomaly_slice(state, index)
    fixed_units = int(state["fixed"]["sum_units"][index])
    fixed_square_units = int(state["fixed"]["square_sum_units"][index])
    fixed_bad = int(state["fixed"]["bad_counts"][index])
    max_abs = float(state["max_abs"][index])
    factor = 10**scale
    if spec.op == "count":
        value: int | float = int(count)
        bits = None
        fixed_num = int(count)
        fixed_den = 1
        error_bound: float | str = 0.0
    else:
        if spec.op == "sum":
            value = sum_float_from_bins(sum_bins, anomaly)
            fixed_num, fixed_den = fixed_units, 1
            error_bound = count * 0.5 / factor if math.isfinite(max_abs) else "Infinity"
        elif spec.op == "mean":
            value = mean_float_from_bins(sum_bins, anomaly, count)
            fixed_num, fixed_den = fixed_units, count * factor
            error_bound = 0.5 / factor if math.isfinite(max_abs) else "Infinity"
        elif spec.op == "var":
            value = variance_float_from_bins(sum_bins, square_bins, anomaly, count)
            variance_fraction = fixed_variance_fraction(
                fixed_units, fixed_square_units, count, scale
            )
            fixed_num, fixed_den = variance_fraction.numerator, variance_fraction.denominator
            unit_error = 0.5 / factor
            error_bound = (
                2.0 * max_abs * unit_error + unit_error * unit_error
                if math.isfinite(max_abs)
                else "Infinity"
            )
        else:
            raise fail("E_QUERY_AGGREGATE", "unsupported aggregate operation", op=spec.op)
        bits = float_bits(float(value))
    if fixed_bad:
        fixed_value: float | str | None = None
        fixed_bits = None
        fixed_available = False
        fixed_reason = []
        if anomaly["fixed_overflow"]:
            fixed_reason.append("E_FIXED_POINT_OVERFLOW")
        if anomaly["nan"] or anomaly["posinf"] or anomaly["neginf"]:
            fixed_reason.append("NONFINITE_INPUT")
    else:
        fixed_fraction = Fraction(fixed_num, fixed_den)
        fixed_value = json_number(fraction_to_float(fixed_fraction))
        fixed_bits = float_bits(fraction_to_float(fixed_fraction))
        fixed_available = True
        fixed_reason = []
    return {
        "name": spec.name,
        "op": spec.op,
        "column": spec.column,
        "value": value,
        "value_bits": bits,
        "count": int(count),
        "error_spectrum": anomaly,
        "float_path": {
            "algorithm": "exact_binary_superaccumulator",
            "rounding": "single_correctly_rounded_conversion",
            "error_upper_bound": 0.0,
        },
        "fixed": {
            "scale": scale,
            "unit": f"1/{factor}",
            "rounding": "nearest_ties_to_even",
            "rounding_direction": "both",
            "available": fixed_available,
            "unavailable_reasons": fixed_reason,
            "value": fixed_value,
            "value_bits": fixed_bits,
            "value_numerator": int(fixed_num),
            "value_denominator": int(fixed_den),
            "overflow_count": int(anomaly["fixed_overflow"]),
            "error_upper_bound": error_bound,
            "steps": [
                "project_column",
                "convert_to_float64_or_exact_integer",
                f"multiply_by_scale={factor}",
                "round_nearest_ties_to_even_to_int64_units" if spec.op != "count" else "count_exact",
                "accumulate_integer_units",
                "single_final_rational_conversion",
            ],
        },
    }


def window_pairs_for_shard(
    batch: Batch,
    query: Query,
    start: int,
    stop: int,
    selected: np.ndarray,
    partition_offsets: dict[str, int],
    partition_totals: dict[str, int],
    window_base_ordinals: dict[str, int],
) -> tuple[np.ndarray, np.ndarray]:
    if query.window is None:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    window = query.window
    ids, keys, _displays, n_parts = partition_rows(
        batch, window.partition_by, start, stop, selected
    )
    row_parts: list[np.ndarray] = []
    row_windows: list[np.ndarray] = []
    max_q = (window.length - 1) // window.step
    for part in range(n_parts):
        local = np.flatnonzero(ids == part)
        if local.size == 0:
            continue
        key = keys[part]
        total = int(partition_totals.get(key, 0))
        offset = int(partition_offsets.get(key, 0))
        max_k = (total - window.length) // window.step
        if max_k < 0:
            continue
        base_ordinal = int(window_base_ordinals[key])
        local_ordinals = np.cumsum(np.ones(local.size, dtype=np.int64)) - 1
        positions = offset + local_ordinals
        for q in range(max_q + 1):
            k = positions // window.step - q
            starts = k * window.step
            valid = (k >= 0) & (k <= max_k) & (positions >= starts) & (positions < starts + window.length)
            if not np.any(valid):
                continue
            row_parts.append(local[valid])
            row_windows.append(base_ordinal + k[valid])
    if not row_parts:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)
    return np.concatenate(row_parts), np.concatenate(row_windows)


def annotate_precision(
    outputs: list[dict[str, Any]],
    metric_specs: tuple[AggregateSpec, ...],
    tolerance: float,
    kind: str,
) -> None:
    for spec in metric_specs:
        if spec.op == "count" or tolerance <= 0:
            continue
        considered: list[tuple[str, dict[str, Any], float]] = []
        for output in outputs:
            metric = output["metrics"].get(spec.name)
            if metric is None:
                continue
            value = metric["value"]
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                continue
            ref = output.get("window_id", output.get("key", ""))
            considered.append((ref, output, float(value)))
        evidence: dict[int, list[dict[str, Any]]] = {id(output): [] for _ref, output, _v in considered}
        for left_index in range(len(considered)):
            left_ref, left_output, left_value = considered[left_index]
            for right_index in range(left_index + 1, len(considered)):
                right_ref, right_output, right_value = considered[right_index]
                difference = abs(left_value - right_value)
                if difference <= 0.0 or difference >= tolerance:
                    continue
                left_metric = left_output["metrics"][spec.name]
                right_metric = right_output["metrics"][spec.name]
                sources = {
                    "shards": sorted(
                        set(left_output.get("sources", {}).get("shards", []))
                        | set(right_output.get("sources", {}).get("shards", []))
                    ),
                    "batches": sorted(
                        set(left_output.get("sources", {}).get("batches", []))
                        | set(right_output.get("sources", {}).get("batches", []))
                    ),
                    "conversion_steps": [
                        "exact_float64_superaccumulator",
                        f"fixed_scale={left_metric['fixed']['scale']}",
                        left_metric["fixed"]["rounding"],
                        "float_vs_fixed_rational_conversion",
                    ],
                }
                item = {
                    "rule": "distinct_values_within_tolerance",
                    "kind": kind,
                    "metric": spec.name,
                    "left": left_ref,
                    "right": right_ref,
                    "left_value_bits": left_metric["value_bits"],
                    "right_value_bits": right_metric["value_bits"],
                    "abs_difference": difference,
                    "tolerance": tolerance,
                    "conclusion_at_risk": "values_are_distinct",
                    "sources": sources,
                }
                evidence[id(left_output)].append(item)
                evidence[id(right_output)].append(item)
        for ref, output, _value in considered:
            items = sorted(
                evidence[id(output)],
                key=lambda item: (item["metric"], item["left"], item["right"]),
            )
            metric = output["metrics"][spec.name]
            metric["precision_sensitive"] = bool(items)
            metric["precision_evidence"] = items
    for output in outputs:
        output["precision_sensitive"] = any(
            metric.get("precision_sensitive", False) for metric in output["metrics"].values()
        )


def metric_sources(partials: Iterable[dict[str, Any]]) -> dict[str, list[Any]]:
    shards: set[str] = set()
    batches: set[int] = set()
    for partial in partials:
        shards.update(partial.get("shards", []))
        batches.update(partial.get("batches", []))
    return {"shards": sorted(shards), "batches": sorted(batches)}
