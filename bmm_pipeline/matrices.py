from __future__ import annotations

import numpy as np

from .jobs import JobSpec


def generate_operands(job: JobSpec) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(job.seed)
    left_shape, right_shape = job.matrix_shapes
    left = rng.standard_normal(left_shape, dtype=np.float64)
    right = rng.standard_normal(right_shape, dtype=np.float64)
    left = left.astype(np.dtype(job.dtype)).astype(np.float64)
    right = right.astype(np.dtype(job.dtype)).astype(np.float64)
    if job.layout == "col":
        left = np.asfortranarray(left)
        right = np.asfortranarray(right)
    return left, right


def float64_reference(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    result = np.matmul(
        np.asarray(left, dtype=np.float64, order="C"),
        np.asarray(right, dtype=np.float64, order="C"),
    )
    return np.asarray(result, dtype=np.float64, order="C")
