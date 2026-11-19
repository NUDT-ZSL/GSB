"""Configuration defaults, validation, and deterministic model parameters."""

from __future__ import annotations

import copy
from typing import Any, Iterable

import numpy as np

from .errors import E_CONFIG, LabError


DEFAULT_LABELS = ("cat", "dog", "vehicle", "indoor", "outdoor")
DEFAULT_SEED = 20261120
DEFAULT_ANNOTATION_SEED = 7319
DEFAULT_PROJECTION_SEED = 314159

UNCERTAIN_STRATEGIES = ("ignore", "positive", "separate")
ZERO_DIVISION_POLICIES = ("zero", "one", "skip", "error")
RESOURCE_POLICIES = {
    "sample_limit_policy": ("error", "truncate"),
    "feature_limit_policy": ("error", "truncate"),
    "memory_limit_policy": ("error", "truncate_samples"),
}


def default_config(
    *,
    seed: int = DEFAULT_SEED,
    labels: Iterable[str] = DEFAULT_LABELS,
    width: int = 12,
    height: int = 12,
    grid_rows: int = 3,
    grid_cols: int = 3,
    uncertain_strategy: str = "ignore",
    zero_division: str = "zero",
    max_samples: int = 64,
    max_feature_dim: int = 256,
    max_resident_matrix_bytes: int = 64 * 1024 * 1024,
    sample_limit_policy: str = "error",
    feature_limit_policy: str = "error",
    memory_limit_policy: str = "error",
) -> dict[str, Any]:
    label_list = list(labels)
    thresholds = {
        label: round(0.45 + 0.02 * index, 10)
        for index, label in enumerate(label_list)
    }
    return {
        "seed": int(seed),
        "labels": label_list,
        "image": {"width": int(width), "height": int(height), "channels": 3},
        "preprocess": {"scale": 255.0, "clip_min": 0.0, "clip_max": 255.0},
        "feature": {
            "grid_rows": int(grid_rows),
            "grid_cols": int(grid_cols),
            "include_mean": True,
            "include_std": True,
        },
        "decision": {
            "thresholds": thresholds,
            "fallback_threshold": 0.5,
            "uncertain_strategy": uncertain_strategy,
        },
        "metrics": {"zero_division": zero_division},
        "limits": {
            "max_samples": int(max_samples),
            "max_feature_dim": int(max_feature_dim),
            "max_resident_matrix_bytes": int(max_resident_matrix_bytes),
            "sample_limit_policy": sample_limit_policy,
            "feature_limit_policy": feature_limit_policy,
            "memory_limit_policy": memory_limit_policy,
        },
    }


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    """Validate and return a defensive deep copy of a configuration."""

    if not isinstance(config, dict):
        raise LabError(E_CONFIG, "config must be a JSON object", stage="config")

    required = (
        "seed",
        "labels",
        "image",
        "preprocess",
        "feature",
        "decision",
        "metrics",
        "limits",
    )
    missing = [key for key in required if key not in config]
    if missing:
        raise LabError(
            E_CONFIG,
            "config is missing required sections",
            stage="config",
            details={"missing": missing},
        )

    if (
        not isinstance(config["seed"], int)
        or isinstance(config["seed"], bool)
        or config["seed"] < 0
    ):
        raise LabError(
            E_CONFIG,
            "seed must be a non-negative integer",
            stage="config",
        )

    labels = config["labels"]
    if (
        not isinstance(labels, list)
        or not labels
        or any(not isinstance(label, str) or not label for label in labels)
        or len(set(labels)) != len(labels)
    ):
        raise LabError(
            E_CONFIG,
            "labels must be a non-empty list of unique non-empty strings",
            stage="config",
        )

    image = config["image"]
    for key in ("width", "height", "channels"):
        value = image.get(key) if isinstance(image, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LabError(
                E_CONFIG,
                f"image.{key} must be a positive integer",
                stage="config",
                details={"field": f"image.{key}"},
            )
    if image["channels"] != 3:
        raise LabError(E_CONFIG, "only RGB images are supported", stage="config")

    feature = config["feature"]
    for key in ("include_mean", "include_std"):
        if not isinstance(feature.get(key), bool):
            raise LabError(
                E_CONFIG,
                f"feature.{key} must be a boolean",
                stage="config",
            )
    if not feature["include_mean"] and not feature["include_std"]:
        raise LabError(
            E_CONFIG,
            "feature extraction must include mean or standard deviation",
            stage="config",
        )
    for key in ("grid_rows", "grid_cols"):
        value = feature.get(key) if isinstance(feature, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LabError(
                E_CONFIG,
                f"feature.{key} must be a positive integer",
                stage="config",
            )
    if feature["grid_rows"] > image["height"] or feature["grid_cols"] > image["width"]:
        raise LabError(
            E_CONFIG,
            "feature grid must not exceed image dimensions",
            stage="config",
        )

    decision = config["decision"]
    thresholds = decision.get("thresholds") if isinstance(decision, dict) else None
    if not isinstance(thresholds, dict) or set(thresholds) != set(labels):
        raise LabError(
            E_CONFIG,
            "decision.thresholds must contain exactly the configured labels",
            stage="config",
        )
    for label in labels:
        threshold = thresholds[label]
        if (
            not isinstance(threshold, (int, float))
            or isinstance(threshold, bool)
            or not np.isfinite(threshold)
            or threshold <= 0
        ):
            raise LabError(
                E_CONFIG,
                "configured thresholds must be finite and positive",
                stage="config",
                label=label,
            )
    fallback = decision.get("fallback_threshold")
    if (
        not isinstance(fallback, (int, float))
        or isinstance(fallback, bool)
        or not np.isfinite(fallback)
        or fallback <= 0
    ):
        raise LabError(
            E_CONFIG,
            "decision.fallback_threshold must be finite and positive",
            stage="config",
        )
    if decision.get("uncertain_strategy") not in UNCERTAIN_STRATEGIES:
        raise LabError(
            E_CONFIG,
            "decision.uncertain_strategy is not supported",
            stage="config",
            details={"allowed": list(UNCERTAIN_STRATEGIES)},
        )

    limits = config["limits"]
    for key in ("max_samples", "max_feature_dim", "max_resident_matrix_bytes"):
        value = limits.get(key) if isinstance(limits, dict) else None
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise LabError(
                E_CONFIG,
                f"limits.{key} must be a positive integer",
                stage="config",
            )
    for key, allowed in RESOURCE_POLICIES.items():
        if limits.get(key) not in allowed:
            raise LabError(
                E_CONFIG,
                f"limits.{key} is not supported",
                stage="config",
                details={"allowed": list(allowed)},
            )

    preprocess = config["preprocess"]
    if not isinstance(preprocess, dict):
        raise LabError(E_CONFIG, "preprocess must be an object", stage="config")
    scale = preprocess.get("scale")
    if (
        not isinstance(scale, (int, float))
        or isinstance(scale, bool)
        or not np.isfinite(scale)
        or scale <= 0
    ):
        raise LabError(E_CONFIG, "preprocess.scale must be finite and positive", stage="config")
    clip_min = preprocess.get("clip_min")
    clip_max = preprocess.get("clip_max")
    if (
        not isinstance(clip_min, (int, float))
        or isinstance(clip_min, bool)
        or not np.isfinite(clip_min)
        or not isinstance(clip_max, (int, float))
        or isinstance(clip_max, bool)
        or not np.isfinite(clip_max)
        or clip_min >= clip_max
    ):
        raise LabError(
            E_CONFIG,
            "preprocess clip bounds must be finite and ordered",
            stage="config",
        )

    return copy.deepcopy(config)


def feature_dimension(config: dict[str, Any]) -> int:
    feature = config["feature"]
    channels = config["image"]["channels"]
    blocks = feature["grid_rows"] * feature["grid_cols"]
    components = int(bool(feature["include_mean"])) + int(bool(feature["include_std"]))
    return blocks * channels * components


def create_projection(
    config: dict[str, Any], *, seed: int = DEFAULT_PROJECTION_SEED
) -> dict[str, Any]:
    """Create the fixed projection payload stored in the snapshot."""

    dimension = feature_dimension(config)
    labels = list(config["labels"])
    rng = np.random.Generator(np.random.PCG64(int(seed)))
    projection_weights = rng.normal(
        0.0, 1.0 / np.sqrt(dimension), size=(dimension, dimension)
    )
    weights = rng.normal(0.0, 0.17, size=(dimension, len(labels)))
    bias = rng.normal(0.0, 0.035, size=(len(labels),))
    return {
        "seed": int(seed),
        "feature_dim": dimension,
        "labels": labels,
        "projection_weights": projection_weights.round(12).tolist(),
        "score_weights": weights.round(12).tolist(),
        "bias": bias.round(12).tolist(),
    }


def validate_projection(projection: dict[str, Any], config: dict[str, Any]) -> None:
    labels = config["labels"]
    dimension = feature_dimension(config)
    if projection.get("labels") != labels:
        raise LabError(
            E_CONFIG,
            "projection label order does not match configuration",
            stage="projection",
        )
    if projection.get("feature_dim") != dimension:
        raise LabError(
            E_CONFIG,
            "projection feature dimension does not match configuration",
            stage="projection",
            details={"expected": dimension, "actual": projection.get("feature_dim")},
        )
    projection_weights = np.asarray(
        projection.get("projection_weights"), dtype=np.float64
    )
    score_weights = np.asarray(projection.get("score_weights"), dtype=np.float64)
    bias = np.asarray(projection.get("bias"), dtype=np.float64)
    if (
        projection_weights.shape != (dimension, dimension)
        or score_weights.shape != (dimension, len(labels))
        or bias.shape != (len(labels),)
    ):
        raise LabError(
            E_CONFIG,
            "projection parameter shapes are invalid",
            stage="projection",
            details={
                "expected_projection_weights": [dimension, dimension],
                "actual_projection_weights": list(projection_weights.shape),
                "expected_score_weights": [dimension, len(labels)],
                "actual_score_weights": list(score_weights.shape),
                "expected_bias": [len(labels)],
                "actual_bias": list(bias.shape),
            },
        )
