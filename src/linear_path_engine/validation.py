"""Cross-strength monotonicity, objective, and version consistency checks."""

from __future__ import annotations

from typing import Any

import numpy as np

from .config import RunConfig
from .data import Dataset
from .errors import (
    E_OBJECTIVE_MISMATCH,
    E_MONOTONICITY,
    E_VERSION_MISMATCH,
    EngineError,
)
from .objective import objective_terms
from .standardization import stats_content_version, transform_with_stats


def _as_coefficients(entry: dict[str, Any], n_features: int) -> np.ndarray:
    values = entry.get("coefficients")
    if not isinstance(values, list) or len(values) != n_features:
        raise EngineError(
            E_OBJECTIVE_MISMATCH,
            "path entry coefficient vector does not match the feature schema",
            stage="PATH_VALIDATION",
            strength=float(entry["strength"]) if "strength" in entry else None,
            strength_index=(
                int(entry["strength_index"]) if "strength_index" in entry else None
            ),
            details={
                "expected_coefficient_count": n_features,
                "actual_coefficient_count": len(values) if isinstance(values, list) else None,
            },
        )
    try:
        coefficients = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise EngineError(
            E_OBJECTIVE_MISMATCH,
            "path entry coefficients are not numeric",
            stage="PATH_VALIDATION",
            strength=float(entry["strength"]) if "strength" in entry else None,
            strength_index=(
                int(entry["strength_index"]) if "strength_index" in entry else None
            ),
        ) from exc
    if not np.all(np.isfinite(coefficients)):
        raise EngineError(
            E_OBJECTIVE_MISMATCH,
            "path entry coefficients contain non-finite values",
            stage="PATH_VALIDATION",
            strength=float(entry["strength"]) if "strength" in entry else None,
            strength_index=(
                int(entry["strength_index"]) if "strength_index" in entry else None
            ),
        )
    return coefficients


def validate_path(
    *,
    config: RunConfig,
    dataset: Dataset,
    path_entries: list[dict[str, Any]],
    stats: dict[str, Any],
    stats_version: str,
) -> dict[str, Any]:
    expected_strengths = set(config.model.strengths_ascending)
    actual_strengths = [float(entry.get("strength", float("nan"))) for entry in path_entries]
    if sorted(actual_strengths) != sorted(expected_strengths):
        raise EngineError(
            E_VERSION_MISMATCH,
            "path strengths do not match the configured path",
            stage="PATH_VALIDATION",
            details={
                "configured_strengths": list(config.model.strengths_ascending),
                "path_strengths": actual_strengths,
            },
        )
    entries_ascending = sorted(path_entries, key=lambda item: float(item["strength"]))
    first_entry = entries_ascending[0] if entries_ascending else None
    content_version = stats_content_version(stats)
    if content_version != stats_version:
        raise EngineError(
            E_VERSION_MISMATCH,
            "standardization statistics content does not match the committed version",
            stage="PATH_VALIDATION",
            strength=float(first_entry["strength"]) if first_entry is not None else None,
            strength_index=(
                int(first_entry["strength_index"]) if first_entry is not None else None
            ),
            details={
                "declared_stats_version": stats_version,
                "content_stats_version": content_version,
            },
        )
    independent_matrix = transform_with_stats(dataset, stats)
    if independent_matrix.shape != (dataset.n_samples, dataset.n_features):
        raise EngineError(
            E_VERSION_MISMATCH,
            "standardized matrix shape does not match the dataset schema",
            stage="PATH_VALIDATION",
            strength=float(first_entry["strength"]) if first_entry is not None else None,
            strength_index=(
                int(first_entry["strength_index"]) if first_entry is not None else None
            ),
            details={
                "matrix_shape": list(independent_matrix.shape),
                "expected_shape": [dataset.n_samples, dataset.n_features],
            },
        )

    previous_norm: float | None = None
    previous_strength: float | None = None
    monotonicity_margin_min = float("inf")
    objective_max_abs_diff = 0.0
    objective_max_relative_diff = 0.0
    for entry in entries_ascending:
        strength = float(entry["strength"])
        strength_index = int(entry["strength_index"])
        if entry.get("stats_version") != stats_version:
            raise EngineError(
                E_VERSION_MISMATCH,
                "path entry was solved with a different standardization statistics version",
                stage="PATH_VALIDATION",
                strength=strength,
                strength_index=strength_index,
                details={
                    "entry_stats_version": entry.get("stats_version"),
                    "committed_stats_version": stats_version,
                },
            )
        if entry.get("precision_mode") != config.precision.mode:
            raise EngineError(
                E_VERSION_MISMATCH,
                "path entry precision mode differs from the configured execution version",
                stage="PATH_VALIDATION",
                strength=strength,
                strength_index=strength_index,
                details={
                    "entry_precision_mode": entry.get("precision_mode"),
                    "configured_precision_mode": config.precision.mode,
                },
            )
        coefficients = _as_coefficients(entry, dataset.n_features)
        intercept = float(entry.get("intercept", 0.0))
        norm = float(np.linalg.norm(coefficients, ord=2))
        if previous_norm is not None:
            tolerance = (
                config.validation.monotonicity_abs_tolerance
                + config.validation.monotonicity_rel_tolerance * max(1.0, previous_norm)
            )
            margin = previous_norm - norm
            monotonicity_margin_min = min(monotonicity_margin_min, margin)
            if norm > previous_norm + tolerance:
                raise EngineError(
                    E_MONOTONICITY,
                    "coefficient norm increased as regularization strength increased",
                    stage="PATH_VALIDATION",
                    strength=strength,
                    strength_index=strength_index,
                    details={
                        "current_strength": strength,
                        "previous_strength": previous_strength,
                        "current_norm": norm,
                        "previous_norm": previous_norm,
                        "allowed_increase": tolerance,
                        "actual_increase": norm - previous_norm,
                    },
                )
        recomputed = objective_terms(
            independent_matrix,
            dataset.targets,
            coefficients,
            intercept,
            strength,
            config.model.l1_ratio,
        )
        stored_objective = float(entry["objective"])
        difference = abs(stored_objective - recomputed["objective"])
        tolerance = (
            config.validation.objective_abs_tolerance
            + config.validation.objective_rel_tolerance
            * max(1.0, abs(recomputed["objective"]))
        )
        relative_difference = difference / max(1.0, abs(recomputed["objective"]))
        objective_max_abs_diff = max(objective_max_abs_diff, difference)
        objective_max_relative_diff = max(objective_max_relative_diff, relative_difference)
        if difference > tolerance:
            raise EngineError(
                E_OBJECTIVE_MISMATCH,
                "stored objective cannot be reproduced from coefficients and standardization statistics",
                stage="PATH_VALIDATION",
                strength=strength,
                strength_index=strength_index,
                details={
                    "stored_objective": stored_objective,
                    "recomputed_objective": float(recomputed["objective"]),
                    "absolute_difference": float(difference),
                    "relative_difference": float(relative_difference),
                    "allowed_difference": float(tolerance),
                },
            )
        previous_norm = norm
        previous_strength = strength

    if monotonicity_margin_min == float("inf"):
        monotonicity_margin_min = 0.0
    return {
        "checked": True,
        "path_length": len(path_entries),
        "stats_version": stats_version,
        "stats_version_consistent": True,
        "monotonicity": {
            "checked": True,
            "order": "strength_ascending",
            "abs_tolerance": config.validation.monotonicity_abs_tolerance,
            "rel_tolerance": config.validation.monotonicity_rel_tolerance,
            "minimum_norm_margin": float(monotonicity_margin_min),
        },
        "objective": {
            "checked": True,
            "abs_tolerance": config.validation.objective_abs_tolerance,
            "rel_tolerance": config.validation.objective_rel_tolerance,
            "max_abs_difference": float(objective_max_abs_diff),
            "max_relative_difference": float(objective_max_relative_diff),
        },
    }


def validate_result_document(
    document: dict[str, Any],
    *,
    config: RunConfig,
    dataset: Dataset,
) -> dict[str, Any]:
    if document.get("config_fingerprint") != config.fingerprint:
        raise EngineError(
            E_VERSION_MISMATCH,
            "result config fingerprint does not match the supplied configuration",
            stage="PATH_VALIDATION",
            details={
                "result_config_fingerprint": document.get("config_fingerprint"),
                "current_config_fingerprint": config.fingerprint,
            },
        )
    if document.get("data_fingerprint") != dataset.fingerprint:
        raise EngineError(
            E_VERSION_MISMATCH,
            "result data fingerprint does not match the supplied dataset",
            stage="PATH_VALIDATION",
            details={
                "result_data_fingerprint": document.get("data_fingerprint"),
                "current_data_fingerprint": dataset.fingerprint,
            },
        )
    try:
        standardization_doc = document["standardization"]
        stats = standardization_doc["stats"]
        stats_version = str(standardization_doc["stats_version"])
        path_entries = document["path"]
    except (KeyError, TypeError) as exc:
        raise EngineError(
            E_VERSION_MISMATCH,
            f"result document is missing required fields: {exc}",
            stage="PATH_VALIDATION",
        ) from exc
    if not isinstance(path_entries, list) or not all(
        isinstance(entry, dict) for entry in path_entries
    ):
        raise EngineError(
            E_VERSION_MISMATCH,
            "result path is not an array of objects",
            stage="PATH_VALIDATION",
        )
    return validate_path(
        config=config,
        dataset=dataset,
        path_entries=path_entries,
        stats=stats,
        stats_version=stats_version,
    )

