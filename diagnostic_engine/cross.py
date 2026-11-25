"""Stage 2a/2b: explicit feature crossings and conditional scaling."""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np

from .errors import cross_def, mem_limit, std_zero
from .precision import Precision


def _encode(values: np.ndarray, encoding: str, precision: Precision) -> np.ndarray:
    values = precision.q(values)
    if encoding == "raw":
        return values
    if encoding == "signed_log1p":
        encoded = np.sign(values) * np.log1p(np.abs(values))
        return precision.q(encoded)
    raise cross_def(f"unsupported cross encoding: {encoding}", encoding=encoding)


def expand_cross_features(
    preprocess_result: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
) -> dict[str, Any]:
    cross_cfg = config.get("cross", {})
    slots = cross_cfg.get("slots", [])
    variables = preprocess_result["variables"]
    matrix = np.asarray(preprocess_result["matrix"], dtype=np.float64)
    column_index = {name: idx for idx, name in enumerate(variables)}
    pattern = cross_cfg.get("column_pattern", "{slot}#{combo}#{op}")
    encoding_rules = cross_cfg.get("encoding_rules", {})

    columns: list[np.ndarray] = []
    metadata: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    for slot in slots:
        slot_name = slot.get("name")
        arity = int(slot.get("arity", 0))
        slot_vars = slot.get("variables", [])
        if arity not in (2, 3):
            raise cross_def("slot arity must be 2 or 3", slot=slot_name, arity=arity)
        missing = [name for name in slot_vars if name not in column_index]
        if missing:
            raise cross_def("slot references unknown variable(s)", slot=slot_name, missing=missing)
        ops = slot.get("ops") or (["product"] if arity == 2 else ["triple_product"])

        for combo in itertools.combinations(slot_vars, arity):
            combo_key = "-".join(combo)
            vectors = [matrix[:, column_index[name]] for name in combo]
            for op in ops:
                if op == "product" and arity == 2:
                    raw = precision.mul(vectors[0], vectors[1])
                elif op == "difference" and arity == 2:
                    raw = precision.sub(vectors[0], vectors[1])
                elif op == "triple_product" and arity == 3:
                    raw = precision.mul(precision.mul(vectors[0], vectors[1]), vectors[2])
                else:
                    raise cross_def(
                        "cross op is incompatible with slot arity",
                        slot=slot_name,
                        op=op,
                        arity=arity,
                    )
                encoding = slot.get("encoding", encoding_rules.get(op, "raw"))
                values = _encode(raw, encoding, precision)
                name = pattern.format(slot=slot_name, combo=combo_key, op=op)
                if name in seen_names:
                    raise cross_def("duplicate expanded column name", column=name)
                seen_names.add(name)
                columns.append(values)
                metadata.append(
                    {
                        "column": name,
                        "slot": slot_name,
                        "combo": list(combo),
                        "op": op,
                        "encoding": encoding,
                        "condition_var": combo[0],
                    }
                )

    if not columns:
        raise cross_def("cross configuration produced no columns", slots=len(slots))

    max_columns = int(cross_cfg.get("max_cross_columns", 10_000))
    if len(columns) > max_columns:
        raise mem_limit(
            "expanded cross column count exceeds configured resident limit",
            columns=len(columns),
            max_cross_columns=max_columns,
        )
    rows = matrix.shape[0]
    max_bytes = int(cross_cfg.get("max_resident_bytes", 10**18))
    estimated_bytes = rows * len(columns) * 8
    if estimated_bytes > max_bytes:
        raise mem_limit(
            "estimated resident matrix bytes exceed limit",
            estimated_bytes=estimated_bytes,
            max_resident_bytes=max_bytes,
        )

    expanded = np.column_stack(columns).astype(np.float64)
    return {
        "window": preprocess_result["window"],
        "v_idx": preprocess_result["v_idx"],
        "columns": [item["column"] for item in metadata],
        "metadata": metadata,
        "expanded_matrix": precision.q(expanded),
        "row_count": rows,
        "column_count": len(metadata),
        "precision": precision.config_view(),
    }


def _rank_condition_bins(condition_values: np.ndarray, bin_count: int) -> np.ndarray:
    rows = condition_values.shape[0]
    order = np.argsort(condition_values, kind="stable")
    bins = np.zeros(rows, dtype=np.int64)
    for rank, row_index in enumerate(order):
        bins[row_index] = min(bin_count - 1, (rank * bin_count) // rows)
    return bins


def standardize_cross_features(
    cross_result: dict[str, Any],
    preprocess_result: dict[str, Any],
    config: dict[str, Any],
    precision: Precision,
) -> dict[str, Any]:
    cross_cfg = config.get("cross", {})
    method = cross_cfg.get("condition_method", "zscore")
    bin_count = max(1, int(cross_cfg.get("condition_bins", 1)))
    expanded = np.asarray(cross_result["expanded_matrix"], dtype=np.float64)
    metadata = cross_result["metadata"]
    variables = preprocess_result["variables"]
    base_matrix = np.asarray(preprocess_result["matrix"], dtype=np.float64)
    base_index = {name: idx for idx, name in enumerate(variables)}
    rows, cols = expanded.shape

    standardized = np.zeros_like(expanded)
    statistics: dict[str, Any] = {"method": method, "condition_bins": bin_count, "columns": {}}

    for col, meta in enumerate(metadata):
        condition_name = meta["condition_var"]
        if condition_name not in base_index:
            raise cross_def("condition variable is unavailable", condition_var=condition_name)
        condition_values = base_matrix[:, base_index[condition_name]]
        condition_bins = _rank_condition_bins(condition_values, bin_count)
        column_stats: dict[str, Any] = {"condition_var": condition_name, "bins": {}}

        for bin_index in range(bin_count):
            mask = condition_bins == bin_index
            values = expanded[mask, col]
            if values.size == 0:
                raise std_zero("conditional standardization bin is empty", column=meta["column"], bin=bin_index)
            if method == "zscore":
                center_arr = precision.mean(values)
                scale_arr = precision.sqrt(precision.var(values))
                scale_name = "std"
            elif method == "robust":
                q25, median, q75 = np.quantile(values, [0.25, 0.5, 0.75], method="linear")
                center_arr = precision.q(median)
                scale_arr = precision.q(np.subtract(q75, q25))
                scale_name = "iqr"
            else:
                raise cross_def(f"unsupported standardization method: {method}", method=method)

            center = float(center_arr)
            scale = float(scale_arr)
            if scale == 0.0:
                raise std_zero(
                    "standardization denominator is zero",
                    column=meta["column"],
                    bin=bin_index,
                    scale_name=scale_name,
                )
            standardized[mask, col] = precision.div(
                precision.sub(expanded[mask, col], center_arr),
                scale_arr,
            )
            column_stats["bins"][str(bin_index)] = {
                "rows": int(mask.sum()),
                "center": center,
                "scale": scale,
                "scale_name": scale_name,
            }

        statistics["columns"][meta["column"]] = column_stats

    standardized = precision.q(standardized)
    return {
        "window": cross_result["window"],
        "v_idx": cross_result["v_idx"],
        "columns": cross_result["columns"],
        "metadata": metadata,
        "features": standardized,
        "statistics": statistics,
        "precision": precision.config_view(),
    }
