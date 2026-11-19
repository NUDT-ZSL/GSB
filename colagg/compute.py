"""Numeric paths and deterministic group/window state transitions."""

from __future__ import annotations

import copy
import math
import sys
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any

from .canonical import canonical_json, sha256_value
from .errors import ColAggError, E_FIXED_OVERFLOW, E_TYPE_MISMATCH
from .schema import Batch, OperatorSpec, Precision


EPSILON = sys.float_info.epsilon
HALF = Decimal("0.5")


def _integer_bounds(precision: Precision) -> tuple[int, int]:
    if precision.int_bits == 32:
        return -(2**31), 2**31 - 1
    return -(2**63), 2**63 - 1


def _fixed_overflow(message: str, **details: Any) -> ColAggError:
    return ColAggError(E_FIXED_OVERFLOW, message, details=details)


def _bounded(value: int, precision: Precision, **details: Any) -> int:
    lower, upper = _integer_bounds(precision)
    if value < lower or value > upper:
        raise _fixed_overflow(
            "fixed-point accumulation exceeded the configured integer width",
            value=str(value),
            int_bits=precision.int_bits,
            **details,
        )
    return value


def convert_fixed(value: int | float, precision: Precision) -> tuple[int, str]:
    """Convert one scalar to signed fixed point using deterministic half-even."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ColAggError(
            E_TYPE_MISMATCH,
            "fixed-point conversion requires an integer or float",
            details={"value_type": type(value).__name__},
        )
    if isinstance(value, float) and not math.isfinite(value):
        raise ColAggError(E_TYPE_MISMATCH, "fixed-point conversion requires a finite value")
    raw_decimal = Decimal(value) if isinstance(value, int) else Decimal(repr(value))
    scaled_decimal = raw_decimal * precision.scale
    rounded = scaled_decimal.to_integral_value(rounding=ROUND_HALF_EVEN)
    scaled = int(rounded)
    if rounded > scaled_decimal:
        direction = "up"
    elif rounded < scaled_decimal:
        direction = "down"
    else:
        direction = "none"
    _bounded(
        scaled,
        precision,
        stage="value_conversion",
        raw=repr(value),
        scale=precision.scale,
    )
    return scaled, direction


def _gamma(factor: int) -> float:
    if factor <= 0:
        return 0.0
    value = factor * EPSILON
    if value >= 1.0:
        return sys.float_info.max
    return value / (1.0 - value)


def _float_add_bound(abs_sum: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return _gamma(count - 1) * max(abs_sum, 0.0)


def _float_square_bound(abs_square_sum: float, count: int) -> float:
    if count <= 0:
        return 0.0
    return _gamma(count - 1) * max(abs_square_sum, 0.0)


def _safe_variance(sum_sq: float, total: float, count: int) -> float:
    if count <= 0:
        return 0.0
    mean = total / count
    value = sum_sq / count - mean * mean
    return max(0.0, value)


def _fixed_summary(group: dict[str, Any], precision: Precision) -> dict[str, Any]:
    count = int(group["count"])
    scaled_sum = int(group["fixed_sum"])
    scaled_sum_sq = int(group["fixed_sum_sq"])
    scale = precision.scale
    if count:
        mean = scaled_sum / (scale * count)
        variance_fraction_num = count * scaled_sum_sq - scaled_sum * scaled_sum
        variance = variance_fraction_num / (count * count * scale * scale)
        variance = max(0.0, variance)
    else:
        mean = 0.0
        variance = 0.0
        variance_fraction_num = 0
    return {
        "sum": scaled_sum if scale == 1 else scaled_sum / scale,
        "sum_exact": str(scaled_sum),
        "sum_sq": scaled_sum_sq if scale == 1 else scaled_sum_sq / (scale * scale),
        "sum_sq_exact": str(scaled_sum_sq),
        "mean_exact": f"{scaled_sum}/{scale * count}" if count else "0/1",
        "variance_exact": (
            f"{variance_fraction_num}/{count * count * scale * scale}"
            if count
            else "0/1"
        ),
        "count": count,
        "mean": mean,
        "variance": variance,
    }


def _float_summary(group: dict[str, Any]) -> dict[str, Any]:
    count = int(group["count"])
    total = float(group["float_sum"])
    total_sq = float(group["float_sum_sq"])
    mean = total / count if count else 0.0
    variance = _safe_variance(total_sq, total, count)
    return {
        "sum": total,
        "sum_sq": total_sq,
        "count": count,
        "mean": mean,
        "variance": variance,
    }


def _metric(summary: dict[str, Any], op: str) -> int | float:
    if op == "count":
        return int(summary["count"])
    return summary[op]


def _fixed_error_bound(
    group: dict[str, Any],
    op: str,
    precision: Precision,
) -> float:
    count = int(group["count"])
    if not count or op == "count":
        return 0.0
    delta = 0.5 / precision.scale
    maximum = float(group["max_abs"])
    if op == "sum":
        return count * delta
    if op == "mean":
        return delta
    if op == "variance":
        return 2.0 * maximum * delta + delta * delta
    return 0.0


def _float_error_bound(
    group: dict[str, Any],
    op: str,
    float_summary: dict[str, Any],
) -> float:
    count = int(group["count"])
    if not count or op == "count":
        return 0.0
    abs_sum = float(group["abs_sum"])
    abs_square_sum = float(group["abs_square_sum"])
    sum_bound = _float_add_bound(abs_sum, count)
    square_bound = _float_square_bound(abs_square_sum, count)
    if op == "sum":
        return sum_bound
    if op == "mean":
        mean_bound = sum_bound / count
        magnitude = max(abs(float_summary["mean"]), abs_sum / count, 1e-300)
        return mean_bound + EPSILON * magnitude
    if op == "variance":
        mean = float(float_summary["mean"])
        magnitude = max(abs_square_sum / count, mean * mean, abs(float_summary["variance"]), 1e-300)
        return 2.0 * square_bound / count + EPSILON * magnitude
    return 0.0


def new_group(group_values: list[Any]) -> dict[str, Any]:
    return {
        "group": group_values,
        "count": 0,
        "float_sum": 0.0,
        "float_sum_sq": 0.0,
        "fixed_sum": 0,
        "fixed_sum_sq": 0,
        "abs_sum": 0.0,
        "abs_square_sum": 0.0,
        "max_abs": 0.0,
        "batches": [],
    }


def _batch_chain_entry(group: dict[str, Any], seq: int, column: str | None) -> dict[str, Any]:
    for entry in reversed(group["batches"]):
        if entry["batch_seq"] == seq:
            return entry
    entry = {
        "column": column,
        "batch_seq": seq,
        "count": 0,
        "float_sum": 0.0,
        "float_sum_sq": 0.0,
        "scaled_sum": 0,
        "scaled_sum_sq": 0,
        "rounding": {"up": 0, "down": 0, "none": 0},
        "running": {},
    }
    group["batches"].append(entry)
    return entry


def add_group_value(
    group: dict[str, Any],
    *,
    raw_value: int | float | None,
    seq: int,
    column: str | None,
    precision: Precision,
    trace: list[dict[str, Any]] | None,
    group_key: str,
    batch_row_index: int,
    fixed_details: dict[str, Any] | None = None,
) -> None:
    group["count"] = int(group["count"]) + 1
    entry = _batch_chain_entry(group, seq, column)
    entry["count"] = int(entry["count"]) + 1
    if raw_value is None:
        return
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise ColAggError(
            E_TYPE_MISMATCH,
            "aggregate input is not numeric",
            details={"column": column, "value_type": type(raw_value).__name__},
        )
    raw_float = float(raw_value)
    if not math.isfinite(raw_float):
        raise ColAggError(E_TYPE_MISMATCH, "aggregate input is not finite")
    scaled, direction = convert_fixed(raw_value, precision)
    group["float_sum"] = float(group["float_sum"]) + raw_float
    group["float_sum_sq"] = float(group["float_sum_sq"]) + raw_float * raw_float
    if not math.isfinite(float(group["float_sum"])) or not math.isfinite(
        float(group["float_sum_sq"])
    ):
        raise ColAggError(
            E_FIXED_OVERFLOW,
            "float64 accumulation overflowed",
            details={"batch_seq": seq, "column": column, "group_key": group_key},
        )
    group["fixed_sum"] = _bounded(
        int(group["fixed_sum"]) + scaled,
        precision,
        stage="sum",
        batch_seq=seq,
        column=column,
        group_key=group_key,
    )
    group["fixed_sum_sq"] = _bounded(
        int(group["fixed_sum_sq"]) + scaled * scaled,
        precision,
        stage="sum_sq",
        batch_seq=seq,
        column=column,
        group_key=group_key,
    )
    abs_value = abs(raw_float)
    group["abs_sum"] = float(group["abs_sum"]) + abs_value
    group["abs_square_sum"] = float(group["abs_square_sum"]) + raw_float * raw_float
    group["max_abs"] = max(float(group["max_abs"]), abs_value)
    entry["float_sum"] = float(entry["float_sum"]) + raw_float
    entry["float_sum_sq"] = float(entry["float_sum_sq"]) + raw_float * raw_float
    entry["scaled_sum"] = int(entry["scaled_sum"]) + scaled
    entry["scaled_sum_sq"] = int(entry["scaled_sum_sq"]) + scaled * scaled
    entry["rounding"][direction] += 1
    entry["running"] = {
        "float_sum": float(group["float_sum"]),
        "float_sum_sq": float(group["float_sum_sq"]),
        "fixed_sum": int(group["fixed_sum"]),
        "fixed_sum_sq": int(group["fixed_sum_sq"]),
    }
    if trace is not None:
        trace.append(
            {
                "operator_id": None,
                "batch_seq": seq,
                "column": column,
                "row_index": batch_row_index,
                "raw_value": repr(raw_value),
                "scale": precision.scale,
                "scaled_value": str(scaled),
                "direction": direction,
                "path": "fixed",
                "group_key": group_key,
                "details": fixed_details or {},
            }
        )
        trace.append(
            {
                "operator_id": None,
                "batch_seq": seq,
                "column": column,
                "row_index": batch_row_index,
                "raw_value": repr(raw_value),
                "scale": precision.scale,
                "scaled_value": repr(raw_value),
                "direction": "none",
                "path": "float64",
                "group_key": group_key,
                "details": {"identity_conversion": True},
            }
        )


def finalize_group_record(
    group: dict[str, Any],
    operator: OperatorSpec,
    precision: Precision,
    *,
    watermark_seq: int,
) -> dict[str, Any]:
    fixed = _fixed_summary(group, precision)
    floating = _float_summary(group)
    fixed_value = _metric(fixed, operator.op)
    float_value = _metric(floating, operator.op)
    primary_path = precision.accumulator
    fixed_bound = _fixed_error_bound(group, operator.op, precision)
    float_bound = _float_error_bound(group, operator.op, floating)
    primary_value = fixed_value if primary_path == "fixed" else float_value
    record: dict[str, Any] = {
        "operator_id": operator.operator_id,
        "name": operator.name,
        "kind": "aggregate",
        "op": operator.op,
        "column": operator.column,
        "group": group["group"],
        "group_key": canonical_json(group["group"]),
        "count": int(group["count"]),
        "watermark_seq": watermark_seq,
        "primary_path": primary_path,
        "value": primary_value,
        "float_value": float_value,
        "fixed_value": fixed_value,
        "path_delta": abs(float_value - fixed_value),
        "error_bound": fixed_bound if primary_path == "fixed" else float_bound,
        "bounds": {"fixed": fixed_bound, "float64": float_bound},
        "paths": {
            "float64": floating,
            "fixed": fixed,
        },
        "error_chain": group["batches"],
        "precision_sensitive": False,
        "sensitivity": [],
    }
    record["result_digest"] = sha256_value(record)
    return record


def group_key_for(row: dict[str, Any], group_by: tuple[str, ...]) -> tuple[str, list[Any]]:
    values = [row[name] for name in group_by]
    return canonical_json(values), values


def _row_maps(batch: Batch) -> list[dict[str, Any]]:
    columns = batch.column_map
    rows: list[dict[str, Any]] = []
    for row_index in range(batch.row_count):
        rows.append(
            {
                column_name: column.values[row_index]
                for column_name, column in columns.items()
            }
        )
    return rows


def empty_aggregate_state(operator: OperatorSpec) -> dict[str, Any]:
    return {
        "kind": "aggregate",
        "operator_id": operator.operator_id,
        "groups": {},
        "rows_seen": 0,
    }


def apply_aggregate_batch(
    operator: OperatorSpec,
    precision: Precision,
    state: dict[str, Any],
    batch: Batch,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    next_state = copy.deepcopy(state)
    groups: dict[str, Any] = next_state["groups"]
    output_records: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    touched: set[str] = set()
    for batch_row_index, row in enumerate(_row_maps(batch)):
        group_key, group_values = group_key_for(row, operator.group_by)
        if group_key not in groups:
            groups[group_key] = new_group(group_values)
        group = groups[group_key]
        raw_value = row[operator.column] if operator.column is not None else None
        add_group_value(
            group,
            raw_value=raw_value,
            seq=batch.seq,
            column=operator.column,
            precision=precision,
            trace=traces,
            group_key=group_key,
            batch_row_index=batch_row_index,
            fixed_details={"group": group_values},
        )
        touched.add(group_key)
    next_state["rows_seen"] = int(next_state.get("rows_seen", 0)) + batch.row_count
    for group_key in sorted(touched):
        output_records.append(
            finalize_group_record(
                groups[group_key],
                operator,
                precision,
                watermark_seq=batch.seq,
            )
        )
    for trace in traces:
        trace["operator_id"] = operator.operator_id
    return next_state, output_records, output_records, traces


def empty_window_state(operator: OperatorSpec) -> dict[str, Any]:
    return {
        "kind": "window",
        "operator_id": operator.operator_id,
        "groups": {},
        "rows_seen": 0,
        "finalized": False,
    }


def _new_window_group(group_values: list[Any]) -> dict[str, Any]:
    return {
        "group": group_values,
        "total_rows": 0,
        "next_start": 0,
        "emitted": 0,
        "buffer": [],
        "finalized": False,
    }


def _window_row(
    row: dict[str, Any],
    *,
    batch: Batch,
    batch_row_index: int,
    group_row_index: int,
    operator: OperatorSpec,
    precision: Precision,
) -> dict[str, Any]:
    raw = row[operator.column] if operator.column is not None else None
    if raw is None:
        return {
            "row_index": group_row_index,
            "batch_seq": batch.seq,
            "batch_row_index": batch_row_index,
            "raw": None,
            "scaled": None,
            "scale": precision.scale,
            "direction": "none",
        }
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ColAggError(
            E_TYPE_MISMATCH,
            "window input is not numeric",
            details={"column": operator.column, "batch_seq": batch.seq},
        )
    if isinstance(raw, float) and not math.isfinite(raw):
        raise ColAggError(E_TYPE_MISMATCH, "window input is not finite")
    scaled, direction = convert_fixed(raw, precision)
    return {
        "row_index": group_row_index,
        "batch_seq": batch.seq,
        "batch_row_index": batch_row_index,
        "raw": raw,
        "scaled": scaled,
        "scale": precision.scale,
        "direction": direction,
    }


def _window_record(
    operator: OperatorSpec,
    precision: Precision,
    *,
    group_values: list[Any],
    group_key: str,
    window_index: int,
    start_row: int,
    rows: list[dict[str, Any]],
    complete: bool,
    boundary: str,
) -> dict[str, Any]:
    accumulator = new_group(group_values)
    chain: list[dict[str, Any]] = []
    for row in rows:
        scaled_mode = row["scaled"]
        raw = row["raw"]
        add_group_value(
            accumulator,
            raw_value=raw,
            seq=int(row["batch_seq"]),
            column=operator.column,
            precision=precision,
            trace=None,
            group_key=group_key,
            batch_row_index=int(row["batch_row_index"]),
        )
        chain.append(
            {
                "column": operator.column,
                "batch_seq": int(row["batch_seq"]),
                "row_index": int(row["row_index"]),
                "raw": raw,
                "scaled": scaled_mode,
                "scale": int(row["scale"]),
                "direction": row["direction"],
                "running": {
                    "float_sum": float(accumulator["float_sum"]),
                    "fixed_sum": int(accumulator["fixed_sum"]),
                },
            }
        )
    fixed = _fixed_summary(accumulator, precision)
    floating = _float_summary(accumulator)
    fixed_value = _metric(fixed, operator.op)
    float_value = _metric(floating, operator.op)
    fixed_bound = _fixed_error_bound(accumulator, operator.op, precision)
    float_bound = _float_error_bound(accumulator, operator.op, floating)
    primary_path = precision.accumulator
    primary_value: int | float | None
    status = "complete" if complete else ("partial" if boundary == "emit" else "insufficient")
    if not complete and boundary == "mark":
        primary_value = None
    else:
        primary_value = fixed_value if primary_path == "fixed" else float_value
    record: dict[str, Any] = {
        "operator_id": operator.operator_id,
        "name": operator.name,
        "kind": "window",
        "op": operator.op,
        "column": operator.column,
        "group": group_values,
        "group_key": group_key,
        "window_index": window_index,
        "start_row": start_row,
        "end_row": start_row + len(rows) - 1,
        "length": len(rows),
        "configured_length": int(operator.window["length"]),
        "step": int(operator.window["step"]),
        "complete": complete,
        "status": status,
        "primary_path": primary_path,
        "value": primary_value,
        "float_value": float_value,
        "fixed_value": fixed_value,
        "path_delta": abs(float_value - fixed_value),
        "error_bound": fixed_bound if primary_path == "fixed" else float_bound,
        "bounds": {"fixed": fixed_bound, "float64": float_bound},
        "paths": {"float64": floating, "fixed": fixed},
        "error_chain": chain,
        "precision_sensitive": False,
        "sensitivity": [],
    }
    record["result_digest"] = sha256_value(record)
    return record


def _emit_available_windows(
    operator: OperatorSpec,
    precision: Precision,
    group_key: str,
    group_state: dict[str, Any],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    length = int(operator.window["length"])
    step = int(operator.window["step"])
    while int(group_state["total_rows"]) >= int(group_state["next_start"]) + length:
        rows = list(group_state["buffer"][:length])
        records.append(
            _window_record(
                operator,
                precision,
                group_values=group_state["group"],
                group_key=group_key,
                window_index=int(group_state["emitted"]),
                start_row=int(group_state["next_start"]),
                rows=rows,
                complete=True,
                boundary=operator.window["boundary"],
            )
        )
        group_state["buffer"] = group_state["buffer"][step:]
        group_state["next_start"] = int(group_state["next_start"]) + step
        group_state["emitted"] = int(group_state["emitted"]) + 1
    return records


def apply_window_batch(
    operator: OperatorSpec,
    precision: Precision,
    state: dict[str, Any],
    batch: Batch,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    next_state = copy.deepcopy(state)
    groups: dict[str, Any] = next_state["groups"]
    records: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    for batch_row_index, row in enumerate(_row_maps(batch)):
        group_key, group_values = group_key_for(row, operator.group_by)
        if group_key not in groups:
            groups[group_key] = _new_window_group(group_values)
        group_state = groups[group_key]
        group_row_index = int(group_state["total_rows"])
        item = _window_row(
            row,
            batch=batch,
            batch_row_index=batch_row_index,
            group_row_index=group_row_index,
            operator=operator,
            precision=precision,
        )
        group_state["buffer"].append(item)
        group_state["total_rows"] = group_row_index + 1
        traces.append(
            {
                "operator_id": operator.operator_id,
                "batch_seq": batch.seq,
                "column": operator.column,
                "row_index": batch_row_index,
                "raw_value": repr(row[operator.column]) if operator.column is not None else "null",
                "scale": precision.scale,
                "scaled_value": str(item["scaled"]) if item["scaled"] is not None else "null",
                "direction": item["direction"],
                "path": "fixed",
                "group_key": group_key,
                "details": {"group_row_index": group_row_index},
            }
        )
        traces.append(
            {
                "operator_id": operator.operator_id,
                "batch_seq": batch.seq,
                "column": operator.column,
                "row_index": batch_row_index,
                "raw_value": (
                    repr(row[operator.column]) if operator.column is not None else "null"
                ),
                "scale": precision.scale,
                "scaled_value": (
                    repr(row[operator.column]) if operator.column is not None else "null"
                ),
                "direction": "none",
                "path": "float64",
                "group_key": group_key,
                "details": {
                    "group_row_index": group_row_index,
                    "identity_conversion": True,
                },
            }
        )
        records.extend(_emit_available_windows(operator, precision, group_key, group_state))
    next_state["rows_seen"] = int(next_state.get("rows_seen", 0)) + batch.row_count
    records.sort(key=lambda item: (item["group_key"], item["window_index"]))
    return next_state, records, records, traces


def finalize_window_state(
    operator: OperatorSpec,
    precision: Precision,
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    next_state = copy.deepcopy(state)
    if next_state.get("finalized"):
        return next_state, []
    records: list[dict[str, Any]] = []
    length = int(operator.window["length"])
    step = int(operator.window["step"])
    boundary = operator.window["boundary"]
    for group_key in sorted(next_state["groups"]):
        group_state = next_state["groups"][group_key]
        while int(group_state["next_start"]) < int(group_state["total_rows"]):
            available = int(group_state["total_rows"]) - int(group_state["next_start"])
            actual = min(length, available)
            rows = list(group_state["buffer"][:actual])
            if boundary != "skip":
                records.append(
                    _window_record(
                        operator,
                        precision,
                        group_values=group_state["group"],
                        group_key=group_key,
                        window_index=int(group_state["emitted"]),
                        start_row=int(group_state["next_start"]),
                        rows=rows,
                        complete=False,
                        boundary=boundary,
                    )
                )
            group_state["buffer"] = group_state["buffer"][step:]
            group_state["next_start"] = int(group_state["next_start"]) + step
            group_state["emitted"] = int(group_state["emitted"]) + 1
        group_state["finalized"] = True
    next_state["finalized"] = True
    records.sort(key=lambda item: (item["group_key"], item["window_index"]))
    return next_state, records

