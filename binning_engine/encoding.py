"""Feature specification, target statistics and deterministic row encoding."""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from .binning import ComparisonMeter, assign_row_bins
from .codes import EngineError
from .hashing import canonical_hash
from .quant import merge_error_intervals, quantize_values
from .validation import validate_row

try:
    import numpy as np
except ImportError:  # pragma: no cover - environment guarantees NumPy.
    np = None  # type: ignore


def precision_cast(values: Sequence[float], tier: str) -> List[float]:
    if tier == "float32":
        if np is None:
            raise EngineError("E_CONFIG_INVALID", "numpy is required for float32 precision tier")
        return [float(np.float32(value)) for value in values]
    return [float(value) for value in values]


def build_feature_spec(
    config: Mapping[str, Any], boundary_result: Mapping[str, Any]
) -> Dict[str, Any]:
    blocks: List[Dict[str, Any]] = []
    offset = 0
    for column in config["numeric_columns"]:
        spec = config["column_specs"][column]
        effective_bins = int(boundary_result["columns"][column]["bin_count_effective"])
        method = spec["encoding_method"]
        length = effective_bins if method == "one_hot" else 1
        block = {
            "column": column,
            "method": method,
            "offset": offset,
            "length": length,
            "bin_count_effective": effective_bins,
            "quant_scale": float(spec["quant_scale"]),
            "zero_point": int(spec["zero_point"]),
        }
        blocks.append(block)
        offset += length
    feature_spec = {
        "blocks": blocks,
        "dimension": offset,
        "precision_tier": config["precision_tier"],
    }
    feature_spec["spec_hash"] = canonical_hash(
        {key: value for key, value in feature_spec.items() if key != "spec_hash"}
    )
    return feature_spec


def build_encoding_context(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    boundary_result: Mapping[str, Any],
) -> Dict[str, Any]:
    meter = ComparisonMeter()
    global_label_sum = sum(float(row[config["label_column"]]) for row in rows)
    global_label_count = len(rows)
    global_mean = global_label_sum / global_label_count
    target_columns = [
        column
        for column in config["numeric_columns"]
        if config["column_specs"][column]["encoding_method"] == "target_encoding"
    ]

    target_bin_stats: Dict[str, Any] = {}
    bin_encodings: Dict[str, List[float]] = {}
    bin_quantized: Dict[str, List[int]] = {}
    bin_dequantized: Dict[str, List[float]] = {}
    intervals = []

    for column in target_columns:
        effective_bins = int(boundary_result["columns"][column]["bin_count_effective"])
        sums = [0.0 for _ in range(effective_bins)]
        counts = [0 for _ in range(effective_bins)]
        for row in rows:
            bins = assign_row_bins(row, boundary_result, config, meter)
            bin_index = bins[column]
            sums[bin_index] += float(row[config["label_column"]])
            counts[bin_index] += 1

        smoothing = float(config["column_specs"][column]["smoothing"])
        means: List[float] = []
        for bin_index in range(effective_bins):
            count = counts[bin_index]
            if count == 0 and smoothing == 0.0:
                means.append(global_mean)
            else:
                means.append(
                    (sums[bin_index] + smoothing * global_mean) / (count + smoothing)
                )
        tier = config["precision_tier"]
        precision_means = precision_cast(means, "float64" if tier == "int8" else tier)
        scale = float(config["column_specs"][column]["quant_scale"])
        zero_point = int(config["column_specs"][column]["zero_point"])
        quant_result = quantize_values(precision_means, scale, zero_point)
        target_bin_stats[column] = {
            "bin_sums": sums,
            "bin_counts": counts,
            "global_label_sum": global_label_sum,
            "global_label_count": global_label_count,
            "global_mean": global_mean,
            "smoothing": smoothing,
        }
        bin_encodings[column] = precision_means
        bin_quantized[column] = list(quant_result["quantized"])  # type: ignore[arg-type]
        bin_dequantized[column] = list(quant_result["dequantized"])  # type: ignore[arg-type]
        intervals.append(quant_result["error_interval"])

    context = {
        "global_mean": global_mean,
        "target_columns": target_columns,
        "target_bin_stats": target_bin_stats,
        "bin_encodings": bin_encodings,
        "bin_quantized": bin_quantized,
        "bin_dequantized": bin_dequantized,
        "quantization_error": merge_error_intervals(intervals),
        "comparison_count": meter.count,
    }
    context["context_hash"] = canonical_hash(
        {key: value for key, value in context.items() if key != "context_hash"}
    )
    return context


def _encode_single_row(
    row: Mapping[str, Any],
    config: Mapping[str, Any],
    boundary_result: Mapping[str, Any],
    feature_spec: Mapping[str, Any],
    context: Mapping[str, Any],
    meter: ComparisonMeter,
) -> Dict[str, Any]:
    validate_row(row, config)
    bins = assign_row_bins(row, boundary_result, config, meter)
    dimension = int(feature_spec["dimension"])
    raw_vector: List[float] = [0.0 for _ in range(dimension)]
    sparse_indices: List[int] = []

    for block in feature_spec["blocks"]:
        column = block["column"]
        offset = int(block["offset"])
        length = int(block["length"])
        bin_index = int(bins[column])
        if block["method"] == "one_hot":
            if not 0 <= bin_index < length:
                raise EngineError(
                    "E_BIN_INDEX",
                    "computed bin index is outside feature block",
                    row_id=row.get("row_id"),
                    details={"column": column, "bin_index": bin_index, "length": length},
                )
            global_index = offset + bin_index
            raw_vector[global_index] = 1.0
            sparse_indices.append(global_index)
        else:
            raw_value = float(context["bin_encodings"][column][bin_index])
            raw_vector[offset] = raw_value

    quantized_vector: List[int] = []
    dequantized_vector: List[float] = []
    interval_parts = []
    for block in feature_spec["blocks"]:
        offset = int(block["offset"])
        length = int(block["length"])
        scale = float(block["quant_scale"])
        zero_point = int(block["zero_point"])
        block_raw = raw_vector[offset : offset + length]
        tier = config["precision_tier"]
        if tier == "float32":
            precision_input = precision_cast(block_raw, "float32")
        else:
            precision_input = [float(value) for value in block_raw]
        quant_result = quantize_values(precision_input, scale, zero_point)
        quantized_vector.extend(quant_result["quantized"])  # type: ignore[arg-type]
        dequantized_vector.extend(quant_result["dequantized"])  # type: ignore[arg-type]
        interval_parts.append(quant_result["error_interval"])

    tier = config["precision_tier"]
    if tier == "float64":
        feature_vector = [float(value) for value in raw_vector]
    elif tier == "float32":
        feature_vector = precision_cast(raw_vector, "float32")
    else:
        feature_vector = list(dequantized_vector)

    return {
        "row_id": row["row_id"],
        "bins": bins,
        "sparse_indices": sorted(sparse_indices),
        "quantized_vector": quantized_vector,
        "dequantized_vector": dequantized_vector,
        "feature_vector": feature_vector,
        "error_interval": merge_error_intervals(interval_parts),
    }


def encode_rows(
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    boundary_result: Mapping[str, Any],
    feature_spec: Mapping[str, Any],
    context: Mapping[str, Any],
    *,
    batch_id: Optional[str] = None,
    isolate_failures: bool = True,
    resource_decisions: Optional[Sequence[Mapping[str, Any]]] = None,
) -> Dict[str, Any]:
    meter = ComparisonMeter()
    row_results: List[Dict[str, Any]] = []
    row_errors: List[Dict[str, Any]] = []
    business_codes: List[str] = []

    for row in rows:
        row_id = row.get("row_id") if isinstance(row, Mapping) else None
        try:
            result = _encode_single_row(
                row, config, boundary_result, feature_spec, context, meter
            )
            row_results.append(result)
        except EngineError as error:
            error.batch_id = batch_id
            if error.row_id is None and row_id is not None:
                error.row_id = row_id
            row_errors.append({"ok": False, "error": error.to_dict()})
            business_codes.append(error.code)
            if not isolate_failures:
                raise
        except Exception as error:  # defensive worker error attribution.
            engine_error = EngineError(
                "E_WORKER",
                f"worker encoding failure: {error}",
                row_id=row_id,
                batch_id=batch_id,
            )
            row_errors.append({"ok": False, "error": engine_error.to_dict()})
            business_codes.append(engine_error.code)
            if not isolate_failures:
                raise engine_error from error

    intervals = [item["error_interval"] for item in row_results]
    resource_decisions = [dict(item) for item in (resource_decisions or [])]
    resource_codes = [str(item.get("code")) for item in resource_decisions if item.get("code")]
    batch = {
        "batch_id": batch_id,
        "ok": not row_errors,
        "row_count": len(rows),
        "success_count": len(row_results),
        "error_count": len(row_errors),
        "row_ids": [row.get("row_id") for row in rows if isinstance(row, Mapping)],
        "successful_row_ids": [item["row_id"] for item in row_results],
        "row_results": row_results,
        "row_errors": row_errors,
        "resource_decisions": resource_decisions,
        "business_codes": sorted(set(business_codes) | set(resource_codes)),
        "comparison_count": meter.count,
        "error_interval": merge_error_intervals(intervals),
    }
    batch["batch_hash"] = canonical_hash(
        {key: value for key, value in batch.items() if key != "batch_hash"}
    )
    return batch


def encode_batch_worker(task: Dict[str, Any]) -> Dict[str, Any]:
    """Top-level process-pool entry point; task and result are picklable data."""

    try:
        return encode_rows(
            task["rows"],
            task["config"],
            task["boundary_result"],
            task["feature_spec"],
            task["context"],
            batch_id=task.get("batch_id"),
            isolate_failures=bool(task.get("isolate_failures", True)),
            resource_decisions=task.get("resource_decisions", []),
        )
    except EngineError as error:
        return {
            "batch_id": task.get("batch_id"),
            "ok": False,
            "batch_error": error.to_dict(),
            "row_results": [],
            "row_errors": [],
            "comparison_count": 0,
        }
    except Exception as error:
        wrapped = EngineError(
            "E_WORKER",
            f"batch worker failed: {error}",
            batch_id=task.get("batch_id"),
        )
        return {
            "batch_id": task.get("batch_id"),
            "ok": False,
            "batch_error": wrapped.to_dict(),
            "row_results": [],
            "row_errors": [],
            "comparison_count": 0,
        }
