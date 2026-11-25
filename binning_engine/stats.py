"""Deterministic feature statistics and cross-artifact self-checks."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Sequence

from .cache import ContentCache
from .hashing import canonical_hash
from .quant import merge_error_intervals


def _numeric_summary(values: Sequence[float]) -> Dict[str, float]:
    count = len(values)
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    return {
        "count": count,
        "min": min(values),
        "max": max(values),
        "mean": mean,
        "population_std": math.sqrt(variance),
    }


def build_statistics(
    *,
    rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
    boundaries: Mapping[str, Any],
    feature_spec: Mapping[str, Any],
    context: Mapping[str, Any],
    encoded_rows: Sequence[Mapping[str, Any]],
    encoding_result_hash: str,
    row_errors: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    encoded_by_id = {row["row_id"]: row for row in encoded_rows}
    column_statistics: Dict[str, Any] = {}

    for column in config["numeric_columns"]:
        values = [float(row[column]) for row in rows]
        bin_distribution: Dict[str, int] = {}
        for row in rows:
            encoded = encoded_by_id[row["row_id"]]
            bin_index = int(encoded["bins"][column])
            key = str(bin_index)
            bin_distribution[key] = bin_distribution.get(key, 0) + 1
        boundary_info = boundaries["columns"][column]
        item: Dict[str, Any] = {
            "value_summary": _numeric_summary(values),
            "bin_method": boundary_info["bin_method"],
            "bin_count_requested": boundary_info["bin_count_requested"],
            "bin_count_effective": boundary_info["bin_count_effective"],
            "boundaries": boundary_info["boundaries"],
            "bin_distribution": {
                key: bin_distribution.get(key, 0)
                for key in sorted(bin_distribution, key=lambda value: int(value))
            },
            "encoding_method": config["column_specs"][column]["encoding_method"],
            "quant_scale": config["column_specs"][column]["quant_scale"],
            "zero_point": config["column_specs"][column]["zero_point"],
        }
        if column in context.get("target_bin_stats", {}):
            item["target_encoding"] = context["target_bin_stats"][column]
            item["target_bin_encodings"] = context["bin_encodings"][column]
            item["target_bin_quantized"] = context["bin_quantized"][column]
            item["target_bin_dequantized"] = context["bin_dequantized"][column]
        column_statistics[column] = item

    categorical_statistics: Dict[str, Any] = {}
    for column in config["categorical_columns"]:
        counts: Dict[str, int] = {}
        for row in rows:
            value = str(row[column])
            counts[value] = counts.get(value, 0) + 1
        categorical_statistics[column] = {
            "vocabulary": list(config["vocabulary"][column]),
            "counts": {key: counts[key] for key in sorted(counts)},
        }

    label_values = [float(row[config["label_column"]]) for row in rows]
    intervals = [row["error_interval"] for row in encoded_rows]
    intervals.append(context.get("quantization_error", {}))
    quantization_error = merge_error_intervals(
        [interval for interval in intervals if interval]
    )
    total_nonzeros = sum(len(row["sparse_indices"]) for row in encoded_rows)
    comparison_count = int(context.get("comparison_count", 0)) + sum(
        int(row.get("_comparison_count", 0)) for row in []
    )

    stats = {
        "schema": "binning-feature-statistics/v1",
        "version": int(config["version"]),
        "config_name": config["config_name"],
        "config_version": int(config["version"]),
        "sample_content_hash": boundaries.get("sample_content_hash"),
        "boundary_hash": boundaries["boundary_hash"],
        "feature_spec_hash": feature_spec["spec_hash"],
        "encoding_context_hash": context["context_hash"],
        "encoding_result_hash": encoding_result_hash,
        "row_count": len(rows),
        "encoded_row_count": len(encoded_rows),
        "row_error_count": len(row_errors),
        "encoding_dimension": feature_spec["dimension"],
        "sparse_nonzero_count": total_nonzeros,
        "comparison_count_note": "batch comparison totals are in runtime_report",
        "label_summary": _numeric_summary(label_values),
        "numeric_column_statistics": column_statistics,
        "categorical_column_statistics": categorical_statistics,
        "quantization_error": quantization_error,
        "business_codes": sorted(
            {code for error in row_errors for code in [error["error"]["code"]]}
        ),
    }
    stats["statistics_hash"] = canonical_hash(
        {key: value for key, value in stats.items() if key != "statistics_hash"}
    )
    return stats


def cross_check_cache_and_artifacts(
    cache: ContentCache,
    *,
    encoding_result: Mapping[str, Any],
    statistics: Mapping[str, Any],
) -> Dict[str, Any]:
    cache_report = cache.consistency_report()
    hash_checks = {
        "encoding_result_hash_matches": encoding_result.get("result_hash")
        == canonical_hash(
            {k: v for k, v in encoding_result.items() if k != "result_hash"}
        ),
        "statistics_hash_matches": statistics.get("statistics_hash")
        == canonical_hash({k: v for k, v in statistics.items() if k != "statistics_hash"}),
        "linked_encoding_hash_matches": statistics.get("encoding_result_hash")
        == encoding_result.get("result_hash"),
    }
    ok = cache_report["ok"] and all(hash_checks.values())
    return {"ok": ok, "cache_consistency": cache_report, "hash_checks": hash_checks}
