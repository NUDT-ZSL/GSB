from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from .canonical import sha256_obj
from .config import WorkbenchConfig
from .precision import PrecisionOps


@dataclass
class StandardizedData:
    values: np.ndarray
    stats: dict[str, Any]
    stats_hash: str
    values_hash: str


def compute_standardization(
    raw_features: np.ndarray,
    config: WorkbenchConfig,
    ops: PrecisionOps,
) -> StandardizedData:
    raw = ops.cast(np.asarray(raw_features, dtype=np.float64))
    n_samples, dimensions = raw.shape
    if n_samples <= 0:
        raise ValueError("standardization requires at least one sample")
    if config.normalization == "zscore":
        totals = ops.sum(raw, axis=0)
        means = ops.div(totals, float(n_samples))
        centered = ops.sub(raw, means)
        squares = ops.mul(centered, centered)
        variances = ops.div(ops.sum(squares, axis=0), float(n_samples))
        safe_variances = np.maximum(
            np.asarray(variances, dtype=np.float64), config.variance_floor
        )
        standard_deviations = ops.sqrt(safe_variances)
        values = ops.div(centered, standard_deviations)
        stats: dict[str, Any] = {
            "normalization": "zscore",
            "n_samples": int(n_samples),
            "dimensions": int(dimensions),
            "means": [float(value) for value in means],
            "standard_deviations": [
                float(value) for value in standard_deviations
            ],
            "variances": [float(value) for value in variances],
            "variance_floor": float(config.variance_floor),
            "precision": ops.metadata.to_dict(),
        }
    else:
        minima = ops.cast(np.min(raw, axis=0))
        maxima = ops.cast(np.max(raw, axis=0))
        ranges = ops.sub(maxima, minima)
        safe_ranges = np.maximum(
            np.asarray(ranges, dtype=np.float64), config.variance_floor
        )
        values = ops.div(ops.sub(raw, minima), safe_ranges)
        stats = {
            "normalization": "minmax",
            "n_samples": int(n_samples),
            "dimensions": int(dimensions),
            "minima": [float(value) for value in minima],
            "maxima": [float(value) for value in maxima],
            "ranges": [float(value) for value in ranges],
            "range_floor": float(config.variance_floor),
            "precision": ops.metadata.to_dict(),
        }
    values = ops.cast(values)
    stats_hash = sha256_obj(stats)
    values_hash = sha256_obj(
        {
            "stats_hash": stats_hash,
            "values": values.tolist(),
            "precision": ops.metadata.to_dict(),
        }
    )
    return StandardizedData(
        values=values,
        stats=stats,
        stats_hash=stats_hash,
        values_hash=values_hash,
    )


def transform_one(
    feature: list[float] | np.ndarray,
    stats: dict[str, Any],
    ops: PrecisionOps,
) -> np.ndarray:
    row = ops.cast(np.asarray(feature, dtype=np.float64).reshape(1, -1))
    if stats["normalization"] == "zscore":
        means = ops.cast(np.asarray(stats["means"], dtype=np.float64))
        stds = ops.cast(
            np.asarray(stats["standard_deviations"], dtype=np.float64)
        )
        return ops.cast(ops.div(ops.sub(row, means), stds))
    minima = ops.cast(np.asarray(stats["minima"], dtype=np.float64))
    ranges = ops.cast(np.asarray(stats["ranges"], dtype=np.float64))
    return ops.cast(ops.div(ops.sub(row, minima), ranges))


def transform_many(
    raw_features: np.ndarray,
    stats: dict[str, Any],
    ops: PrecisionOps,
) -> np.ndarray:
    raw = ops.cast(np.asarray(raw_features, dtype=np.float64))
    if stats["normalization"] == "zscore":
        means = ops.cast(np.asarray(stats["means"], dtype=np.float64))
        stds = ops.cast(
            np.asarray(stats["standard_deviations"], dtype=np.float64)
        )
        return ops.cast(ops.div(ops.sub(raw, means), stds))
    minima = ops.cast(np.asarray(stats["minima"], dtype=np.float64))
    ranges = ops.cast(np.asarray(stats["ranges"], dtype=np.float64))
    return ops.cast(ops.div(ops.sub(raw, minima), ranges))
