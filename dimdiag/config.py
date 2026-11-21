"""Configuration loading, defaults, and strict validation."""

from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Mapping

from .errors import E_CONFIG_INVALID, E_INTERMEDIATE_DIM, BusinessError, config_error


SUPPORTED_METHODS = ("pca", "random_projection", "pca_random_projection")
SUPPORTED_PRECISIONS = ("float64", "float32", "fixed")
SUPPORTED_ROUNDING = ("half_even", "half_away_from_zero", "truncate")
SUPPORTED_THRESHOLD_STRATEGIES = ("strict", "tolerance_band")
SUPPORTED_BOUNDARY_DECISIONS = ("accept", "reject")
SUPPORTED_OVERFLOW_POLICIES = ("error", "degrade_random_projection")


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": 1,
    "dataset": {
        "kind": "csv",
        "path": "data/demo.csv",
        "row_id_column": "row_id",
        "label_column": "label",
        "numeric_columns": [],
        "categorical_columns": [],
    },
    "method": "pca",
    "target_dim": 4,
    "standardize": True,
    "random_projection": {
        "seed": 1701,
        "density": 0.45,
    },
    "two_stage": {
        "intermediate_dim": 8,
        "seed": 1901,
        "density": 0.55,
    },
    "precision": {
        "mode": "float64",
        "fixed_scale": 1024,
        "rounding": "half_even",
    },
    "evaluation": {
        "k_neighbors": 7,
        "class_consistency_threshold": 0.75,
        "variance_explained_threshold": 0.80,
        "threshold_strategy": "tolerance_band",
        "threshold_tolerance": 0.002,
        "subnormal_floor": 1e-300,
        "boundary_decision": "accept",
    },
    "error_tolerance": {
        "standardization_max_abs": 1.0e-3,
        "standardization_max_rel": 1.0e-3,
        "model_construction_max_abs": 1.0e-3,
        "model_construction_max_rel": 1.0e-3,
        "coordinates_max_abs": 1.0e-3,
        "coordinates_max_rel": 1.0e-3,
        "two_stage_max_abs": 1.0e-3,
        "two_stage_max_rel": 1.0e-3,
    },
    "resources": {
        "max_features": 512,
        "max_dim_matrix_bytes": 64 * 1024 * 1024,
        "max_projection_buffer_bytes": 64 * 1024 * 1024,
        "overflow_policy": "error",
    },
    "diagnostics": {
        "top_error_elements": 8,
    },
}


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = copy.deepcopy(dict(base))
    for key, value in override.items():
        if (
            key in result
            and isinstance(result[key], dict)
            and isinstance(value, Mapping)
        ):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise config_error(field, "expected a boolean", value=value)
    return value


def _require_int(value: Any, field: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise config_error(field, "expected an integer", value=value)
    if minimum is not None and value < minimum:
        raise config_error(
            field, f"must be >= {minimum}", value=value, minimum=minimum
        )
    return value


def _require_number(
    value: Any,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    inclusive_minimum: bool = True,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise config_error(field, "expected a number", value=value)
    number = float(value)
    if not math.isfinite(number):
        raise config_error(field, "must be finite", value=value)
    if minimum is not None:
        if inclusive_minimum and number < minimum:
            raise config_error(field, f"must be >= {minimum}", value=number)
        if not inclusive_minimum and number <= minimum:
            raise config_error(field, f"must be > {minimum}", value=number)
    if maximum is not None and number > maximum:
        raise config_error(field, f"must be <= {maximum}", value=number)
    return number


def _require_choice(value: Any, field: str, choices: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in choices:
        raise config_error(
            field, "unsupported value", value=value, allowed=list(choices)
        )
    return value


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise config_error(field, "expected a non-empty string", value=value)
    return value


def _require_str_list(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise config_error(field, "expected a list of non-empty strings", value=value)
    if len(set(value)) != len(value):
        raise config_error(field, "duplicate column names are not allowed")
    return list(value)


def validate_config(raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise config_error("$", "configuration must be an object")
    config = _deep_merge(DEFAULT_CONFIG, raw)
    if config.get("schema_version") != 1:
        raise config_error(
            "schema_version", "only schema_version=1 is supported"
        )

    dataset = config["dataset"]
    if not isinstance(dataset, Mapping):
        raise config_error("dataset", "expected an object")
    kind = _require_choice(
        dataset.get("kind", "csv"), "dataset.kind", ("csv", "generated_table")
    )
    dataset["kind"] = kind
    dataset["row_id_column"] = _require_str(
        dataset.get("row_id_column", "row_id"), "dataset.row_id_column"
    )
    dataset["label_column"] = _require_str(
        dataset.get("label_column", "label"), "dataset.label_column"
    )
    if kind == "csv":
        dataset["path"] = _require_str(dataset.get("path"), "dataset.path")
        dataset["numeric_columns"] = _require_str_list(
            dataset.get("numeric_columns", []), "dataset.numeric_columns"
        )
        dataset["categorical_columns"] = _require_str_list(
            dataset.get("categorical_columns", []), "dataset.categorical_columns"
        )
        if not dataset["numeric_columns"] and not dataset["categorical_columns"]:
            raise config_error(
                "dataset",
                "at least one numeric or categorical feature column is required",
            )
    else:
        dataset["seed"] = _require_int(
            dataset.get("seed", 20261122), "dataset.seed", 0
        )
        dataset["n_rows"] = _require_int(
            dataset.get("n_rows", 240), "dataset.n_rows", 2
        )
        dataset["n_numeric"] = _require_int(
            dataset.get("n_numeric", 10), "dataset.n_numeric", 1
        )
        dataset["n_categorical"] = _require_int(
            dataset.get("n_categorical", 2), "dataset.n_categorical", 0
        )
        dataset["n_classes"] = _require_int(
            dataset.get("n_classes", 4), "dataset.n_classes", 2
        )
        dataset["class_sep"] = _require_number(
            dataset.get("class_sep", 1.15),
            "dataset.class_sep",
            minimum=0.0,
            inclusive_minimum=False,
        )

    method = _require_choice(config.get("method"), "method", SUPPORTED_METHODS)
    config["method"] = method
    config["target_dim"] = _require_int(config.get("target_dim"), "target_dim", 1)
    config["standardize"] = _require_bool(config.get("standardize"), "standardize")

    projection = config["random_projection"]
    if not isinstance(projection, Mapping):
        raise config_error("random_projection", "expected an object")
    projection["seed"] = _require_int(
        projection.get("seed"), "random_projection.seed", 0
    )
    projection["density"] = _require_number(
        projection.get("density"),
        "random_projection.density",
        minimum=0.0,
        maximum=1.0,
        inclusive_minimum=False,
    )

    two_stage = config["two_stage"]
    if not isinstance(two_stage, Mapping):
        raise config_error("two_stage", "expected an object")
    two_stage["intermediate_dim"] = _require_int(
        two_stage.get("intermediate_dim"),
        "two_stage.intermediate_dim",
        1,
    )
    two_stage["seed"] = _require_int(two_stage.get("seed"), "two_stage.seed", 0)
    two_stage["density"] = _require_number(
        two_stage.get("density"),
        "two_stage.density",
        minimum=0.0,
        maximum=1.0,
        inclusive_minimum=False,
    )
    if method == "pca_random_projection":
        if two_stage["intermediate_dim"] < config["target_dim"]:
            raise BusinessError(
                E_INTERMEDIATE_DIM,
                "two-stage intermediate_dim must be >= target_dim",
                {
                    "field": "two_stage.intermediate_dim",
                    "intermediate_dim": two_stage["intermediate_dim"],
                    "target_dim": config["target_dim"],
                    "reason": "intermediate_below_target",
                },
            )

    precision = config["precision"]
    if not isinstance(precision, Mapping):
        raise config_error("precision", "expected an object")
    precision["mode"] = _require_choice(
        precision.get("mode"), "precision.mode", SUPPORTED_PRECISIONS
    )
    precision["fixed_scale"] = _require_int(
        precision.get("fixed_scale"), "precision.fixed_scale", 1
    )
    precision["rounding"] = _require_choice(
        precision.get("rounding"), "precision.rounding", SUPPORTED_ROUNDING
    )

    evaluation = config["evaluation"]
    if not isinstance(evaluation, Mapping):
        raise config_error("evaluation", "expected an object")
    evaluation["k_neighbors"] = _require_int(
        evaluation.get("k_neighbors"), "evaluation.k_neighbors", 1
    )
    for key in ("class_consistency_threshold", "variance_explained_threshold"):
        evaluation[key] = _require_number(
            evaluation.get(key),
            f"evaluation.{key}",
            minimum=0.0,
            maximum=1.0,
        )
    evaluation["threshold_strategy"] = _require_choice(
        evaluation.get("threshold_strategy"),
        "evaluation.threshold_strategy",
        SUPPORTED_THRESHOLD_STRATEGIES,
    )
    evaluation["threshold_tolerance"] = _require_number(
        evaluation.get("threshold_tolerance"),
        "evaluation.threshold_tolerance",
        minimum=0.0,
    )
    evaluation["subnormal_floor"] = _require_number(
        evaluation.get("subnormal_floor"),
        "evaluation.subnormal_floor",
        minimum=0.0,
        inclusive_minimum=False,
    )
    evaluation["boundary_decision"] = _require_choice(
        evaluation.get("boundary_decision"),
        "evaluation.boundary_decision",
        SUPPORTED_BOUNDARY_DECISIONS,
    )

    tolerance = config["error_tolerance"]
    if not isinstance(tolerance, Mapping):
        raise config_error("error_tolerance", "expected an object")
    for key, value in list(tolerance.items()):
        if not key.endswith(("_max_abs", "_max_rel")):
            raise config_error(
                "error_tolerance",
                "unknown tolerance field",
                field=f"error_tolerance.{key}",
            )
        tolerance[key] = _require_number(
            value,
            f"error_tolerance.{key}",
            minimum=0.0,
            inclusive_minimum=False,
        )

    resources = config["resources"]
    if not isinstance(resources, Mapping):
        raise config_error("resources", "expected an object")
    resources["max_features"] = _require_int(
        resources.get("max_features"), "resources.max_features", 1
    )
    resources["max_dim_matrix_bytes"] = _require_int(
        resources.get("max_dim_matrix_bytes"),
        "resources.max_dim_matrix_bytes",
        1,
    )
    resources["max_projection_buffer_bytes"] = _require_int(
        resources.get("max_projection_buffer_bytes"),
        "resources.max_projection_buffer_bytes",
        1,
    )
    resources["overflow_policy"] = _require_choice(
        resources.get("overflow_policy"),
        "resources.overflow_policy",
        SUPPORTED_OVERFLOW_POLICIES,
    )
    diagnostics = config["diagnostics"]
    if not isinstance(diagnostics, Mapping):
        raise config_error("diagnostics", "expected an object")
    diagnostics["top_error_elements"] = _require_int(
        diagnostics.get("top_error_elements"),
        "diagnostics.top_error_elements",
        1,
    )
    return config


def load_config(path: str | Path) -> dict[str, Any]:
    import json

    source = Path(path)
    try:
        with source.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            "configuration file does not exist",
            {"field": "config_path", "path": str(source)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            E_CONFIG_INVALID,
            "configuration file is not valid JSON",
            {
                "field": "config_path",
                "path": str(source),
                "line": exc.lineno,
                "column": exc.colno,
            },
        ) from exc
    return validate_config(raw)


def precision_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    precision = config["precision"]
    return {
        "mode": precision["mode"],
        "fixed_scale": int(precision["fixed_scale"]),
        "rounding": precision["rounding"],
    }


def method_label(method: str) -> str:
    return {
        "pca": "PCA",
        "random_projection": "随机投影",
        "pca_random_projection": "PCA + 随机投影",
    }[method]
