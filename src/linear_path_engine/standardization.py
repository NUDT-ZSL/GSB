"""Deterministic feature standardization and fixed one-hot encoding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import RunConfig
from .data import Dataset
from .errors import E_DATA, E_VERSION_MISMATCH, EngineError
from .util import sha256_value


_NUMERIC_MIN_SCALE = 1.0e-12


@dataclass(frozen=True)
class StandardizedData:
    matrix: np.ndarray
    stats: dict[str, Any]
    stats_version: str
    feature_names: tuple[str, ...]


def stats_content_version(stats: dict[str, Any]) -> str:
    return sha256_value(
        {
            "contract": "standardization-v1",
            "method": stats["method"],
            "numeric": stats["numeric"],
            "categorical": stats["categorical"],
            "feature_names": stats["feature_names"],
        }
    )


def fit_standardization(dataset: Dataset, config: RunConfig) -> StandardizedData:
    method = config.standardization
    numeric_stats: dict[str, dict[str, Any]] = {}
    for column in config.data.numeric_columns:
        values = dataset.numeric_values[column]
        minimum = float(np.min(values))
        maximum = float(np.max(values))
        if method == "zscore":
            mean = float(np.mean(values))
            std = float(np.std(values, ddof=0))
            scale = std if std > _NUMERIC_MIN_SCALE else 1.0
            numeric_stats[column] = {
                "method": "zscore",
                "mean": mean,
                "std": std,
                "scale": scale,
                "min": minimum,
                "max": maximum,
            }
        else:
            span = maximum - minimum
            scale = span if span > _NUMERIC_MIN_SCALE else 1.0
            numeric_stats[column] = {
                "method": "minmax",
                "mean": float(np.mean(values)),
                "std": float(np.std(values, ddof=0)),
                "scale": scale,
                "min": minimum,
                "max": maximum,
            }
    categorical_stats = {
        column: {
            "encoding": "one_hot_sorted",
            "categories": list(dataset.category_categories[column]),
        }
        for column in config.data.categorical_columns
    }
    stats = {
        "method": method,
        "numeric": numeric_stats,
        "categorical": categorical_stats,
        "feature_names": list(dataset.feature_names),
    }
    matrix = transform_with_stats(dataset, stats)
    version = stats_content_version(stats)
    return StandardizedData(
        matrix=matrix,
        stats=stats,
        stats_version=version,
        feature_names=dataset.feature_names,
    )


def transform_with_stats(dataset: Dataset, stats: dict[str, Any]) -> np.ndarray:
    try:
        method = stats["method"]
        numeric_stats = stats["numeric"]
        categorical_stats = stats["categorical"]
        stored_feature_names = tuple(stats["feature_names"])
    except (KeyError, TypeError) as exc:
        raise EngineError(
            E_VERSION_MISMATCH,
            f"standardization statistics are incomplete: {exc}",
            stage="standardization",
        ) from exc
    if method not in {"zscore", "minmax"}:
        raise EngineError(
            E_VERSION_MISMATCH,
            f"unknown standardization method {method!r}",
            stage="standardization",
        )
    if stored_feature_names != dataset.feature_names:
        raise EngineError(
            E_VERSION_MISMATCH,
            "standardization feature schema does not match the dataset",
            stage="standardization",
            details={
                "stored_feature_count": len(stored_feature_names),
                "dataset_feature_count": len(dataset.feature_names),
            },
        )

    columns: list[np.ndarray] = []
    for column in dataset.feature_names:
        if column.startswith("num:"):
            source_column = column[4:]
            try:
                stats_for_column = numeric_stats[source_column]
                center = float(stats_for_column["mean"] if method == "zscore" else stats_for_column["min"])
                scale = float(stats_for_column["scale"])
            except (KeyError, TypeError, ValueError) as exc:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    f"numeric statistics for {source_column!r} are invalid",
                    stage="standardization",
                ) from exc
            if not np.isfinite(scale) or scale <= 0.0:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    f"numeric scale for {source_column!r} must be finite and positive",
                    stage="standardization",
                )
            columns.append((dataset.numeric_values[source_column] - center) / scale)
        elif column.startswith("cat:"):
            body = column[4:]
            if "=" not in body:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    f"invalid categorical feature name {column!r}",
                    stage="standardization",
                )
            source_column, category = body.split("=", 1)
            try:
                declared_categories = tuple(categorical_stats[source_column]["categories"])
            except (KeyError, TypeError) as exc:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    f"categorical statistics for {source_column!r} are invalid",
                    stage="standardization",
                ) from exc
            if category not in declared_categories:
                raise EngineError(
                    E_VERSION_MISMATCH,
                    f"category {category!r} is absent from stored statistics",
                    stage="standardization",
                )
            observed = dataset.categorical_values[source_column]
            columns.append(np.fromiter((1.0 if item == category else 0.0 for item in observed), dtype=np.float64))
        else:
            raise EngineError(
                E_VERSION_MISMATCH,
                f"unknown feature name {column!r}",
                stage="standardization",
            )
    if not columns:
        raise EngineError(E_DATA, "dataset produced no encoded features", stage="standardization")
    matrix = np.column_stack(columns).astype(np.float64, copy=False)
    if not np.all(np.isfinite(matrix)):
        raise EngineError(
            E_DATA,
            "standardized matrix contains non-finite values",
            stage="standardization",
        )
    return matrix

