"""Fixed acoustic prototypes and deterministic classifier models."""

from __future__ import annotations

from functools import lru_cache

import numpy as np

from .constants import COARSE_EMOTIONS, FEATURE_NAMES, FEATURE_NAMES as _ALL_FEATURES, FINE_EMOTIONS
from .util import stable_hash

# Constants are domain parameters, not values learned from the input directory.
# They keep the demonstrator independent of external corpora while remaining a
# genuine two-path segment classifier.
FEATURE_MEANS = np.asarray(
    [
        177.2, 21.1, 27.9, 249.9, 0.2148, 0.1436, 0.3965,
        0.0473, 0.0197, 0.9900, 4.9200, 0.9140, 0.4240, 0.5760,
    ],
    dtype=np.float64,
)
FEATURE_SCALES = np.asarray(
    [
        45.0, 15.0, 30.0, 130.0, 0.075, 0.050, 0.090,
        0.025, 0.015, 0.010, 1.20, 0.100, 0.050, 0.050,
    ],
    dtype=np.float64,
)
NC_FEATURE_WEIGHTS = np.asarray(
    [1.50, 0.90, 1.00, 0.65, 1.10, 0.80, 1.05, 1.35, 1.15, 0.60, 1.30, 0.80, 0.65, 0.65],
    dtype=np.float64,
)

PROTOTYPES = {
    "coarse": {
        "negative": (177.5, 24.0, 33.1, 286.3, 0.2013, 0.1330, 0.3685, 0.0505, 0.0270, 0.9885, 4.833, 0.903, 0.428, 0.566),
        "neutral": (156.3, 11.5, 4.5, 125.1, 0.2290, 0.1490, 0.4120, 0.0310, 0.0150, 0.9890, 4.456, 0.947, 0.409, 0.591),
        "positive": (196.7, 19.1, 31.1, 229.7, 0.2450, 0.1610, 0.4430, 0.0290, 0.0070, 0.9930, 5.737, 0.926, 0.415, 0.585),
    },
    "fine": {
        "angry": (214.8, 19.2, 41.8, 201.3, 0.2310, 0.1530, 0.4260, 0.0340, 0.0100, 0.9910, 5.763, 0.911, 0.426, 0.574),
        "disgust": (141.7, 24.0, 17.3, 287.0, 0.2200, 0.1450, 0.3940, 0.0800, 0.0530, 0.9880, 3.932, 0.884, 0.436, 0.564),
        "fear": (240.1, 23.9, 63.9, 311.8, 0.2290, 0.1470, 0.4070, 0.0560, 0.0270, 0.9900, 6.582, 0.920, 0.403, 0.597),
        "happy": (196.7, 19.1, 31.1, 229.7, 0.2450, 0.1610, 0.4430, 0.0290, 0.0070, 0.9930, 5.737, 0.926, 0.415, 0.585),
        "neutral": (156.3, 11.5, 4.5, 125.1, 0.2290, 0.1490, 0.4120, 0.0310, 0.0150, 0.9890, 4.456, 0.947, 0.409, 0.591),
        "sad": (113.4, 29.0, 9.3, 345.2, 0.1250, 0.0870, 0.2470, 0.0320, 0.0180, 0.9850, 3.056, 0.896, 0.448, 0.552),
    },
}


def categories_for(granularity: str) -> tuple[str, ...]:
    if granularity == "coarse":
        return COARSE_EMOTIONS
    if granularity == "fine":
        return FINE_EMOTIONS
    raise ValueError(f"unsupported granularity: {granularity}")


def _feature_indices(feature_names: tuple[str, ...]) -> np.ndarray:
    lookup = {name: index for index, name in enumerate(_ALL_FEATURES)}
    try:
        return np.asarray([lookup[name] for name in feature_names], dtype=np.int64)
    except KeyError as exc:
        raise ValueError(f"unknown feature in model: {exc}") from exc


def _standardize(raw: np.ndarray, indices: np.ndarray) -> np.ndarray:
    return (raw[indices] - FEATURE_MEANS[indices]) / FEATURE_SCALES[indices]


def prototype_matrix(granularity: str, feature_names: tuple[str, ...]) -> tuple[tuple[str, ...], np.ndarray]:
    categories = categories_for(granularity)
    indices = _feature_indices(feature_names)
    rows = []
    for category in categories:
        raw = np.asarray(PROTOTYPES[granularity][category], dtype=np.float64)
        rows.append(_standardize(raw, indices))
    return categories, np.vstack(rows)


def centroid_parameters(granularity: str, feature_names: tuple[str, ...]) -> dict:
    categories, centroids = prototype_matrix(granularity, feature_names)
    weights = NC_FEATURE_WEIGHTS[_feature_indices(feature_names)]
    return {
        "path": "weighted_centroid",
        "categories": categories,
        "centroids": centroids,
        "feature_weights": weights,
        "temperature": 1.65,
    }


def _training_set(
    granularity: str,
    feature_names: tuple[str, ...],
    samples_per_class: int = 11,
) -> tuple[tuple[str, ...], np.ndarray, np.ndarray, np.ndarray]:
    categories, centroids = prototype_matrix(granularity, feature_names)
    features = []
    labels = []
    weights = []
    for class_index, centroid in enumerate(centroids):
        for sample_index in range(samples_per_class):
            offset = np.asarray(
                [
                    (((sample_index + 1) * (feature_index + 3) + class_index * 5) % 9 - 4) / 100.0
                    for feature_index in range(centroid.size)
                ],
                dtype=np.float64,
            )
            features.append(centroid * (1.0 + offset))
            labels.append(class_index)
            weights.append(1.0 / samples_per_class)
    return categories, np.vstack(features), np.asarray(labels, dtype=np.int64), np.asarray(weights)


@lru_cache(maxsize=32)
def _trained_logistic(
    granularity: str, feature_names: tuple[str, ...]
) -> tuple[tuple[str, ...], np.ndarray, np.ndarray, str]:
    categories, x_values, labels, sample_weights = _training_set(granularity, feature_names)
    class_count = len(categories)
    feature_count = len(feature_names)
    y = np.zeros((labels.size, class_count), dtype=np.float64)
    y[np.arange(labels.size), labels] = 1.0
    weights_matrix = np.zeros((class_count, feature_count), dtype=np.float64)
    intercept = np.zeros(class_count, dtype=np.float64)
    learning_rate = 0.55
    l2 = 0.018
    total_weight = float(np.sum(sample_weights))
    for _ in range(650):
        logits = x_values @ weights_matrix.T + intercept
        shifted = logits - np.max(logits, axis=1, keepdims=True)
        exponentials = np.exp(shifted)
        probabilities = exponentials / np.sum(exponentials, axis=1, keepdims=True)
        residual = (probabilities - y) * sample_weights[:, None] / total_weight
        grad_w = residual.T @ x_values + l2 * weights_matrix
        grad_b = np.sum(residual, axis=0)
        weights_matrix -= learning_rate * grad_w
        intercept -= learning_rate * grad_b
    parameter_record = {
        "path": "balanced_weighted_logistic_regression",
        "categories": categories,
        "weights": weights_matrix.tolist(),
        "intercept": intercept.tolist(),
        "training_grid": {"samples_per_class": 11, "iterations": 650, "l2": l2, "learning_rate": learning_rate},
    }
    digest = stable_hash(parameter_record)
    return categories, weights_matrix, intercept, digest


def logistic_parameters(granularity: str, feature_names: tuple[str, ...]) -> dict:
    categories, weights_matrix, intercept, digest = _trained_logistic(granularity, feature_names)
    return {
        "path": "balanced_weighted_logistic_regression",
        "categories": categories,
        "weights": weights_matrix,
        "intercept": intercept,
        "parameters_digest": digest,
    }


def all_feature_names() -> tuple[str, ...]:
    return FEATURE_NAMES
