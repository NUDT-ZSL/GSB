"""Deterministic feature scaling fitted only on training windows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import numpy as np

from .utils import array_digest, ordered_sum
from .windows import WindowCollection, ordered_training_rows


@dataclass
class FeatureScaler:
    means: np.ndarray
    stds: np.ndarray
    method: str
    compensated_sum: bool

    def transform(self, values: np.ndarray) -> np.ndarray:
        array = np.asarray(values, dtype=np.float64)
        return np.ascontiguousarray((array - self.means) / self.stds, dtype=np.float64)

    def transform_row(self, values: np.ndarray) -> np.ndarray:
        return self.transform(np.asarray(values, dtype=np.float64).reshape(1, -1))[0]

    def summary(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "compensated_sum": self.compensated_sum,
            "means": [float(value) for value in self.means],
            "stds": [float(value) for value in self.stds],
            "digest": array_digest(
                np.ascontiguousarray(np.vstack([self.means, self.stds]))
            ),
        }


def fit_feature_scaler(
    collection: WindowCollection,
    config: Mapping[str, Any],
) -> FeatureScaler:
    training_matrix, _, _ = ordered_training_rows(collection)
    feature_count = training_matrix.shape[1]
    features_config = config["features"]
    training_config = config["training"]
    if not features_config["standardize"]:
        return FeatureScaler(
            means=np.zeros(feature_count, dtype=np.float64),
            stds=np.ones(feature_count, dtype=np.float64),
            method="none",
            compensated_sum=bool(training_config["compensated_sum"]),
        )

    means = np.empty(feature_count, dtype=np.float64)
    stds = np.empty(feature_count, dtype=np.float64)
    compensated = bool(training_config["compensated_sum"])
    for column in range(feature_count):
        values = training_matrix[:, column]
        mean = ordered_sum((float(value) for value in values), compensated) / len(values)
        squared = ordered_sum(
            ((float(value) - mean) * (float(value) - mean) for value in values),
            compensated,
        ) / len(values)
        means[column] = mean
        std = float(np.sqrt(squared))
        stds[column] = std if std > np.finfo(np.float64).eps else 1.0
    return FeatureScaler(
        means=means,
        stds=stds,
        method="zscore_training_rows",
        compensated_sum=compensated,
    )
