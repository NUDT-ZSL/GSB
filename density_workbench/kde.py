from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import WorkbenchConfig
from .precision import PrecisionOps


@dataclass
class KDEChunkContribution:
    raw_sum: np.ndarray
    kernel_evaluations: int


def kde_chunk_contributions(
    queries: np.ndarray,
    sources: np.ndarray,
    config: WorkbenchConfig,
    ops: PrecisionOps,
) -> KDEChunkContribution:
    q = ops.cast(np.asarray(queries, dtype=np.float64))
    s = ops.cast(np.asarray(sources, dtype=np.float64))
    differences = ops.sub(q[:, None, :], s[None, :, :])
    squared = ops.sum(ops.mul(differences, differences), axis=2)
    denominator = ops.cast(2.0 * float(config.bandwidth) ** 2)
    exponent = ops.div(ops.mul(squared, -1.0), denominator)
    kernels = ops.exp(exponent)
    raw_sum = ops.sum(kernels, axis=1)
    return KDEChunkContribution(
        raw_sum=ops.cast(raw_sum),
        kernel_evaluations=int(q.shape[0] * s.shape[0]),
    )


def kde_normalization(
    n_samples: int,
    dimensions: int,
    bandwidth: float,
    ops: PrecisionOps,
) -> float:
    factor = float(n_samples) * (
        2.0 * math.pi * float(bandwidth) ** 2
    ) ** (float(dimensions) / 2.0)
    return ops.cast(1.0 / factor).item()


def reduce_kde_chunks(
    contributions: list[np.ndarray],
    n_samples: int,
    dimensions: int,
    bandwidth: float,
    ops: PrecisionOps,
) -> tuple[np.ndarray, np.ndarray]:
    if not contributions:
        shape = (0,)
        return np.empty(shape), np.empty(shape)
    stacked = np.stack(
        [
            np.atleast_1d(ops.cast(value))
            for value in contributions
        ],
        axis=1,
    )
    raw_sum = ops.sum(stacked, axis=1)
    normalization = kde_normalization(n_samples, dimensions, bandwidth, ops)
    densities = ops.mul(raw_sum, normalization)
    return ops.cast(raw_sum), ops.cast(densities)


def density_confidence(
    density: float,
    bandwidth: float,
    n_samples: int,
    dimensions: int,
) -> dict[str, Any]:
    safe_density = max(float(density), np.finfo(np.float64).tiny)
    return {
        "bandwidth": float(bandwidth),
        "sample_count": int(n_samples),
        "dimensions": int(dimensions),
        "log_density": float(math.log(safe_density)),
        "relative_uncertainty": float(
            bandwidth / math.sqrt(max(n_samples, 1))
        ),
    }
