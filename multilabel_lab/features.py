"""Deterministic pixel preprocessing, block statistics, and projection."""

from __future__ import annotations

from typing import Any

import numpy as np

from .errors import E_PIXEL_RANGE, E_PROJECTION_SHAPE, E_SAMPLE, LabError


def validate_image_shape(image: np.ndarray, config: dict[str, Any]) -> None:
    expected = (
        config["image"]["height"],
        config["image"]["width"],
        config["image"]["channels"],
    )
    if image.shape != expected:
        raise LabError(
            E_SAMPLE,
            "image shape does not match configuration",
            stage="preprocess",
            details={"expected": list(expected), "actual": list(image.shape)},
        )


def preprocess_image(
    image: np.ndarray,
    config: dict[str, Any],
) -> tuple[np.ndarray, list[dict[str, Any]]]:
    """Validate pixel range, clamp, and scale to [0, 1].

    Invalid pixels are reported so the caller can attribute and record a
    degraded path. The returned array is always safe for downstream stages.
    """

    validate_image_shape(image, config)
    preprocess = config["preprocess"]
    lower = float(preprocess["clip_min"])
    upper = float(preprocess["clip_max"])
    pixels = np.asarray(image, dtype=np.float64)
    invalid_mask = (pixels < lower) | (pixels > upper)
    invalid_positions = np.argwhere(invalid_mask)
    issues: list[dict[str, Any]] = []
    if invalid_positions.size:
        coordinates = [
            [int(position[0]), int(position[1]), int(position[2])]
            for position in invalid_positions[:16]
        ]
        issues.append(
            {
                "code": E_PIXEL_RANGE,
                "message": "pixel values fall outside the configured RGB range",
                "invalid_count": int(invalid_positions.shape[0]),
                "positions_preview": coordinates,
            }
        )
    clipped = np.clip(pixels, lower, upper)
    normalized = np.ascontiguousarray(clipped / float(preprocess["scale"]), dtype=np.float64)
    return normalized, issues


def block_statistics(
    normalized_image: np.ndarray,
    config: dict[str, Any],
) -> np.ndarray:
    """Compute fixed per-block RGB means and standard deviations."""

    feature = config["feature"]
    rows = feature["grid_rows"]
    cols = feature["grid_cols"]
    height, width, _channels = normalized_image.shape
    means: list[np.ndarray] = []
    standard_deviations: list[np.ndarray] = []
    for row in range(rows):
        y0 = row * height // rows
        y1 = (row + 1) * height // rows
        for col in range(cols):
            x0 = col * width // cols
            x1 = (col + 1) * width // cols
            block = normalized_image[y0:y1, x0:x1, :]
            if feature["include_mean"]:
                means.append(np.mean(block, axis=(0, 1), dtype=np.float64))
            if feature["include_std"]:
                standard_deviations.append(np.std(block, axis=(0, 1), dtype=np.float64))
    components = means + standard_deviations
    if not components:
        raise LabError(
            E_PROJECTION_SHAPE,
            "feature configuration produced an empty feature vector",
            stage="feature",
        )
    return np.ascontiguousarray(np.concatenate(components), dtype=np.float64)


def project_features(
    raw_features: np.ndarray,
    projection: dict[str, Any],
    *,
    feature_dim: int,
    labels: list[str],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    projection_weights = np.asarray(projection["projection_weights"], dtype=np.float64)
    score_weights = np.asarray(projection["score_weights"], dtype=np.float64)
    bias = np.asarray(projection["bias"], dtype=np.float64)
    if projection.get("labels") != labels:
        raise LabError(
            E_PROJECTION_SHAPE,
            "projection labels are inconsistent with the classification label order",
            stage="feature",
            details={"expected_labels": labels, "actual_labels": projection.get("labels")},
        )
    if (
        projection_weights.shape != (feature_dim, feature_dim)
        or score_weights.shape != (feature_dim, len(labels))
        or bias.shape != (len(labels),)
    ):
        raise LabError(
            E_PROJECTION_SHAPE,
            "projection parameters have an invalid shape",
            stage="feature",
            details={
                "expected_projection_weights": [feature_dim, feature_dim],
                "actual_projection_weights": list(projection_weights.shape),
                "expected_score_weights": [feature_dim, len(labels)],
                "actual_score_weights": list(score_weights.shape),
                "expected_bias": [len(labels)],
                "actual_bias": list(bias.shape),
            },
        )
    if raw_features.shape != (feature_dim,):
        raise LabError(
            E_PROJECTION_SHAPE,
            "raw feature dimension does not match projection weights",
            stage="feature",
            details={
                "expected": [feature_dim],
                "actual": list(raw_features.shape),
            },
        )
    projected = np.ascontiguousarray(
        raw_features @ projection_weights, dtype=np.float64
    )
    return projected, score_weights, bias


def score_features(
    features: np.ndarray,
    weights: np.ndarray,
    bias: np.ndarray,
    labels: list[str],
) -> np.ndarray:
    if features.shape != (weights.shape[0],):
        raise LabError(
            E_PROJECTION_SHAPE,
            "feature vector and score weights are misaligned",
            stage="score",
            details={
                "feature_shape": list(features.shape),
                "weight_shape": list(weights.shape),
            },
        )
    if weights.shape[1] != len(labels) or bias.shape != (len(labels),):
        raise LabError(
            E_PROJECTION_SHAPE,
            "score output does not align one-to-one with configured labels",
            stage="score",
            details={"labels": labels, "weight_shape": list(weights.shape)},
        )
    scores = np.ascontiguousarray(features @ weights + bias, dtype=np.float64)
    return scores
