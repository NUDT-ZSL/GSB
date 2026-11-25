"""Configuration normalization and semantic validation."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .codes import EngineError, ErrorCode
from .hashing import read_json
from .quant import validate_quant_params


ENCODING_ALIASES = {
    "one_hot": "one_hot",
    "one-hot": "one_hot",
    "onehot": "one_hot",
    "sparse_one_hot": "one_hot",
    "target": "target_encoding",
    "target_encoding": "target_encoding",
    "target-encoding": "target_encoding",
    "targetencoding": "target_encoding",
    "mean_encoding": "target_encoding",
}

BIN_METHODS = {"equal_width", "equal_freq"}
PRECISION_TIERS = {
    "float64": "float64",
    "strict": "float64",
    "high": "float64",
    "float32": "float32",
    "balanced": "float32",
    "int8": "int8",
    "quantized": "int8",
}


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: Any, name: str, code: str = ErrorCode.E_CONFIG_INVALID) -> int:
    if not _is_int(value) or value <= 0:
        raise EngineError(code, f"{name} must be a positive integer", {"value": value})
    return int(value)


def normalize_encoding_method(value: Any) -> str:
    if not isinstance(value, str):
        raise EngineError(
            ErrorCode.E_CODE_METHOD,
            "encoding_method must be a string",
            {"encoding_method": value},
        )
    key = value.strip().lower().replace(" ", "_")
    if key not in ENCODING_ALIASES:
        raise EngineError(
            ErrorCode.E_CODE_METHOD,
            f"unknown encoding method: {value}",
            {"encoding_method": value, "supported": ["one_hot", "target_encoding"]},
        )
    return ENCODING_ALIASES[key]


def normalize_precision_tier(value: Any) -> str:
    if value is None:
        return "int8"
    if not isinstance(value, str):
        raise EngineError(
            ErrorCode.E_CONFIG_INVALID, "precision_tier must be a string", {"value": value}
        )
    key = value.strip().lower().replace("-", "_")
    if key not in PRECISION_TIERS:
        raise EngineError(
            ErrorCode.E_CONFIG_INVALID,
            "unknown precision tier",
            {"precision_tier": value, "supported": sorted(set(PRECISION_TIERS.values()))},
        )
    return PRECISION_TIERS[key]


def _adapt_flat_v1(raw: Mapping[str, Any]) -> Dict[str, Any]:
    data = dict(raw)
    overrides: Dict[str, Any] = {}
    for key in ("column_overrides", "overrides"):
        value = data.get(key)
        if isinstance(value, Mapping):
            overrides.update(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, Mapping):
                    column_name = item.get("name", item.get("column"))
                    if column_name:
                        overrides[str(column_name)] = {
                            k: v
                            for k, v in item.items()
                            if k not in {"name", "column"}
                        }

    column_params = data.get("column_params")
    if isinstance(column_params, list):
        for item in column_params:
            if isinstance(item, Mapping):
                column_name = item.get("name", item.get("column"))
                if column_name:
                    overrides.setdefault(str(column_name), {}).update(
                        {
                            k: v
                            for k, v in item.items()
                            if k not in {"name", "column"}
                        }
                    )

    numeric_columns = list(data.get("numeric_columns", []))
    bins: Dict[str, Any] = {
        "default": {
            "bin_count": data.get("bin_count", 8),
            "bin_method": data.get("bin_method", "equal_width"),
        }
    }
    encoding: Dict[str, Any] = {
        "method": data.get("encoding_method", "one_hot"),
        "smoothing": data.get("smoothing", 10.0),
        "column_methods": {},
    }
    quantization: Dict[str, Any] = {
        "default": {
            "quant_scale": data.get("quant_scale", 0.1),
            "zero_point": data.get("zero_point", 0),
        },
        "columns": {},
    }
    for name in numeric_columns:
        override = dict(overrides.get(name, {}))
        bins[name] = {
            "bin_count": override.pop("bin_count", bins["default"]["bin_count"]),
            "bin_method": override.pop("bin_method", bins["default"]["bin_method"]),
        }
        if "encoding_method" in override:
            encoding["column_methods"][name] = override.pop("encoding_method")
        if "smoothing" in override:
            encoding.setdefault("column_smoothing", {})[name] = override.pop("smoothing")
        if "quant_scale" in override or "zero_point" in override:
            quantization["columns"][name] = {
                "quant_scale": override.pop(
                    "quant_scale", quantization["default"]["quant_scale"]
                ),
                "zero_point": override.pop(
                    "zero_point", quantization["default"]["zero_point"]
                ),
            }
    data["bins"] = bins
    data["encoding"] = encoding
    data["quantization"] = quantization
    data["schema_version"] = "1.0-flat-adapted"
    return data


def _budget(data: Mapping[str, Any]) -> Dict[str, Any]:
    raw = data.get("resource_budget", data.get("budget", {}))
    if raw is None:
        raw = {}
    if not isinstance(raw, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "resource_budget must be an object")
    raw = dict(raw)
    top_level_aliases = {
        "max_memory_bytes": "max_memory_bytes",
        "memory_limit_bytes": "max_memory_bytes",
        "max_resident_memory_bytes": "max_memory_bytes",
        "max_rss_bytes": "max_memory_bytes",
        "max_parallel_slots": "max_parallel_slots",
        "max_workers": "max_parallel_slots",
        "parallel_slot_limit": "max_parallel_slots",
        "max_comparisons": "max_comparisons",
        "max_comparison_count": "max_comparisons",
        "comparison_limit": "max_comparisons",
        "max_encoding_length": "max_encoding_length",
        "encoding_length_limit": "max_encoding_length",
    }
    for source, target in top_level_aliases.items():
        if source in data and target not in raw:
            raw[target] = data[source]
    budget = {
        "max_memory_bytes": int(raw.get("max_memory_bytes", 512 * 1024 * 1024)),
        "max_parallel_slots": int(raw.get("max_parallel_slots", 4)),
        "max_comparisons": int(raw.get("max_comparisons", 10_000_000)),
        "max_encoding_length": int(raw.get("max_encoding_length", 1_000_000)),
    }
    aliases = {
        "max_rss_bytes": "max_memory_bytes",
        "max_resident_memory_bytes": "max_memory_bytes",
        "memory_limit_bytes": "max_memory_bytes",
        "parallel_slots": "max_parallel_slots",
        "max_workers": "max_parallel_slots",
        "comparison_limit": "max_comparisons",
        "max_comparison_count": "max_comparisons",
        "encoding_length_limit": "max_encoding_length",
    }
    for alias, target in aliases.items():
        if alias in raw:
            budget[target] = int(raw[alias])
    for key, value in budget.items():
        if not _is_int(value) or value <= 0:
            raise EngineError(
                ErrorCode.E_RESOURCE_LIMIT,
                f"resource_budget.{key} must be a positive integer",
                {"key": key, "value": value},
            )
    return budget


def normalize_config(raw: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "configuration must be a JSON object")
    if raw.get("normalized") is True and "column_specs" in raw:
        return dict(raw)

    schema = str(raw.get("schema", raw.get("schema_version", "")))
    if schema in {"binning-config-v1", "v1", "1.0"} or (
        "bin_count" in raw and "bins" not in raw
    ):
        raw = _adapt_flat_v1(raw)

    version = raw.get("version", raw.get("config_version", raw.get("revision", 1)))
    if not _is_int(version) or version <= 0:
        raise EngineError(
            ErrorCode.E_CONFIG_VERSION,
            "configuration version must be a positive monotonically increasing integer",
            {"version": version},
        )

    numeric_columns = [str(x) for x in raw.get("numeric_columns", [])]
    categorical_columns = [str(x) for x in raw.get("categorical_columns", [])]
    if not numeric_columns:
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "at least one numeric column is required")
    if len(set(numeric_columns)) != len(numeric_columns):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "numeric_columns must be unique")
    if len(set(categorical_columns)) != len(categorical_columns):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "categorical_columns must be unique")

    declared_order_numeric = list(numeric_columns)
    numeric_columns = sorted(numeric_columns)
    categorical_columns = sorted(categorical_columns)

    label_column = str(raw.get("label_column", "label"))
    vocabulary_raw = raw.get("vocabulary", {})
    if not isinstance(vocabulary_raw, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "vocabulary must be an object")
    vocabulary: Dict[str, List[str]] = {}
    for column in categorical_columns:
        values = vocabulary_raw.get(column)
        if not isinstance(values, Iterable) or isinstance(values, (str, bytes)):
            raise EngineError(
                ErrorCode.E_CONFIG_INVALID,
                f"vocabulary for categorical column {column!r} must be a list",
                {"column": column},
            )
        vocabulary[column] = sorted(str(value) for value in values)
        if not vocabulary[column]:
            raise EngineError(
                ErrorCode.E_CONFIG_INVALID,
                f"vocabulary for categorical column {column!r} is empty",
                {"column": column},
            )

    bins_raw = raw.get("bins", {})
    if not isinstance(bins_raw, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "bins must be an object")
    default_bins = bins_raw.get("default", raw.get("bin_defaults", {}))
    if not isinstance(default_bins, Mapping):
        default_bins = {}

    encoding_raw = raw.get("encoding", {})
    if not isinstance(encoding_raw, Mapping):
        encoding_raw = {}
    default_method = normalize_encoding_method(
        encoding_raw.get("method", raw.get("encoding_method", "one_hot"))
    )
    smoothing = encoding_raw.get("smoothing", raw.get("smoothing", 10.0))
    if isinstance(smoothing, bool) or not isinstance(smoothing, (int, float)):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "smoothing must be a finite number")
    smoothing = float(smoothing)
    if not math.isfinite(smoothing) or smoothing < 0.0:
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "smoothing must be non-negative and finite")
    column_methods_raw = encoding_raw.get("column_methods", {})
    if not isinstance(column_methods_raw, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "encoding.column_methods must be an object")
    column_smoothing_raw = encoding_raw.get("column_smoothing", {})
    if not isinstance(column_smoothing_raw, Mapping):
        column_smoothing_raw = {}

    quant_raw = raw.get("quantization", {})
    if not isinstance(quant_raw, Mapping):
        quant_raw = {}
    default_quant = quant_raw.get("default", {})
    if not isinstance(default_quant, Mapping):
        default_quant = {}
    default_scale = default_quant.get("quant_scale", raw.get("quant_scale", 0.1))
    default_zero_point = default_quant.get("zero_point", raw.get("zero_point", 0))
    validate_quant_params(default_scale, default_zero_point)
    quant_columns = quant_raw.get("columns", {})
    if not isinstance(quant_columns, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "quantization.columns must be an object")

    column_specs: Dict[str, Dict[str, Any]] = {}
    for column in numeric_columns:
        bin_cfg = bins_raw.get(column, {})
        if bin_cfg is None:
            bin_cfg = {}
        if not isinstance(bin_cfg, Mapping):
            raise EngineError(ErrorCode.E_CONFIG_INVALID, f"bins.{column} must be an object")
        bin_count = bin_cfg.get(
            "bin_count", default_bins.get("bin_count", raw.get("bin_count", 8))
        )
        bin_method = bin_cfg.get(
            "bin_method", default_bins.get("bin_method", raw.get("bin_method", "equal_width"))
        )
        if not _is_int(bin_count) or bin_count <= 0:
            raise EngineError(
                ErrorCode.E_CONFIG_INVALID,
                f"bins.{column}.bin_count must be a positive integer",
                {"column": column, "bin_count": bin_count},
            )
        if bin_method not in BIN_METHODS:
            raise EngineError(
                ErrorCode.E_BIN_METHOD,
                f"unknown bin_method: {bin_method}",
                {"column": column, "bin_method": bin_method, "supported": sorted(BIN_METHODS)},
            )
        method_value = column_methods_raw.get(column, default_method)
        method = normalize_encoding_method(method_value)
        column_smoothing = column_smoothing_raw.get(column, smoothing)
        if not isinstance(column_smoothing, (int, float)) or isinstance(
            column_smoothing, bool
        ):
            raise EngineError(
                ErrorCode.E_CONFIG_INVALID,
                f"encoding.column_smoothing.{column} must be numeric",
            )
        column_quant = quant_columns.get(column, {})
        if column_quant is None:
            column_quant = {}
        if not isinstance(column_quant, Mapping):
            raise EngineError(
                ErrorCode.E_QUANT_PARAM, f"quantization.columns.{column} must be an object"
            )
        scale = column_quant.get("quant_scale", default_scale)
        zero_point = column_quant.get("zero_point", default_zero_point)
        validate_quant_params(scale, zero_point)
        column_specs[column] = {
            "name": column,
            "bin_count": int(bin_count),
            "bin_method": bin_method,
            "encoding_method": method,
            "smoothing": float(column_smoothing),
            "quant_scale": float(scale),
            "zero_point": int(zero_point),
        }

    scheduler_raw = raw.get("scheduler", {})
    if not isinstance(scheduler_raw, Mapping):
        scheduler_raw = {}
    workers = scheduler_raw.get("workers", raw.get("workers", 2))
    batch_size = scheduler_raw.get("batch_size", raw.get("batch_size", 32))
    _positive_int(workers, "scheduler.workers")
    _positive_int(batch_size, "scheduler.batch_size")

    priority = raw.get("priority", ["quality", "memory", "latency"])
    if not isinstance(priority, list) or not priority:
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "priority must be a non-empty list")
    priority = [str(item) for item in priority]
    allowed = {"quality", "memory", "latency"}
    if any(item not in allowed for item in priority) or len(set(priority)) != len(priority):
        raise EngineError(
            ErrorCode.E_CONFIG_INVALID,
            "priority must be a permutation of quality, memory and latency",
            {"priority": priority},
        )
    if set(priority) != allowed:
        priority = priority + [item for item in ("quality", "memory", "latency") if item not in priority]

    degradation = raw.get("degradation_policy", raw.get("degradation", {}))
    if degradation is None:
        degradation = {}
    if not isinstance(degradation, Mapping):
        raise EngineError(ErrorCode.E_CONFIG_INVALID, "degradation_policy must be an object")

    time_info = raw.get("time", {})
    if not isinstance(time_info, Mapping):
        time_info = {}
    origin_ms = raw.get(
        "time_origin_absolute_ms",
        time_info.get("origin_absolute_ms", 1700000000000),
    )
    relative_seconds = raw.get(
        "recorded_at_relative_seconds",
        raw.get("relative_seconds", time_info.get("relative_seconds", 0)),
    )
    absolute_ms = raw.get("absolute_ms", time_info.get("absolute_ms"))

    config = {
        "normalized": True,
        "schema_version": "2.0",
        "config_name": str(raw.get("config_name", "tabular_diagnostic")),
        "version": int(version),
        "seed": raw.get("seed"),
        "declared_row_count": raw.get("row_count", raw.get("declared_row_count")),
        "time_origin_absolute_ms": int(origin_ms),
        "recorded_at_relative_seconds": relative_seconds,
        "absolute_ms": absolute_ms,
        "numeric_columns": numeric_columns,
        "declared_numeric_order": declared_order_numeric,
        "categorical_columns": categorical_columns,
        "label_column": label_column,
        "vocabulary": vocabulary,
        "column_specs": column_specs,
        "encoding": {
            "default_method": default_method,
            "smoothing": smoothing,
        },
        "precision_tier": normalize_precision_tier(raw.get("precision_tier")),
        "scheduler": {"workers": int(workers), "batch_size": int(batch_size)},
        "resource_budget": _budget(raw),
        "priority": priority,
        "degradation_policy": {
            "on_limit": str(degradation.get("on_limit", "degrade")),
            "allow_worker_reduction": bool(degradation.get("allow_worker_reduction", True)),
            "allow_batch_reduction": bool(degradation.get("allow_batch_reduction", True)),
            "allow_bin_reduction": bool(degradation.get("allow_bin_reduction", True)),
            "allow_quant_coarsening": bool(
                degradation.get("allow_quant_coarsening", True)
            ),
        },
        "input_paths": raw.get("input_paths", {}),
    }
    return config


def load_config(path: Path) -> Dict[str, Any]:
    raw = read_json(path)
    return normalize_config(raw)


def effective_config_with_resources(
    config: Mapping[str, Any], resource_plan: Mapping[str, Any]
) -> Dict[str, Any]:
    effective = copy.deepcopy(dict(config))
    effective["scheduler"]["workers"] = int(resource_plan["workers"])
    effective["scheduler"]["batch_size"] = int(resource_plan["batch_size"])
    for name, bin_count in resource_plan.get("bin_counts", {}).items():
        if name in effective["column_specs"]:
            effective["column_specs"][name]["bin_count"] = int(bin_count)
    for name, scale in resource_plan.get("quant_scales", {}).items():
        if name in effective["column_specs"]:
            effective["column_specs"][name]["quant_scale"] = float(scale)
    effective["resource_plan_digest"] = resource_plan.get("plan_id")
    return effective
