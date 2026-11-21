"""Deterministic NumPy sample generation from seeds."""

from __future__ import annotations

import hashlib
from typing import Any

import numpy as np

from .schema import NormalizedJob


def array_sha256(array: np.ndarray) -> str:
    contiguous = np.ascontiguousarray(array)
    return hashlib.sha256(contiguous.tobytes(order="C")).hexdigest()


def generate_arrays(job: NormalizedJob) -> tuple[np.ndarray, np.ndarray | None]:
    """Generate task arrays using only NumPy's seeded generator.

    Inputs and weights are generated as float64 regardless of the requested
    execution dtype. This gives the independent reference path a stable source
    snapshot.
    """

    input_rng = np.random.default_rng(job.seed)
    input_array = input_rng.standard_normal(job.input_shape, dtype=np.float64)
    weight_array: np.ndarray | None = None
    if job.family == "conv":
        assert job.kernel_shape is not None
        weight_rng = np.random.default_rng(job.weight_seed)
        weight_array = weight_rng.standard_normal(
            job.kernel_shape, dtype=np.float64
        )
    return input_array, weight_array


def input_metadata(
    input_array: np.ndarray, weight_array: np.ndarray | None
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "input_shape": list(input_array.shape),
        "input_dtype": str(input_array.dtype),
        "input_sha256": array_sha256(input_array),
    }
    if weight_array is not None:
        metadata.update(
            {
                "weight_shape": list(weight_array.shape),
                "weight_dtype": str(weight_array.dtype),
                "weight_sha256": array_sha256(weight_array),
            }
        )
    return metadata
