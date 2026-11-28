"""Independent dense reference computation and elementwise comparison."""

from __future__ import annotations

from typing import Any

import numpy as np

from .model import NormalizedMatrix
from .spec import OperationSpec


def dense_reference(matrix: NormalizedMatrix) -> dict[str, Any]:
    dense = np.zeros((matrix.rows, matrix.columns), dtype=np.float64)
    for entry in matrix.entries:
        dense[entry.row, entry.column] = float(entry.value)
    interest = np.asarray(matrix.vector.values, dtype=np.float64)
    reference = dense @ interest
    return {
        "method": "independent dense ndarray matrix @ dense interest vector",
        "coordinate_basis": "normalized_zero_based_row_column",
        "matrix_shape": [matrix.rows, matrix.columns],
        "matrix_nonzeros": matrix.nonzero_count,
        "vector_length": matrix.columns,
        "vector": [float(value) for value in reference],
    }


def compare_with_reference(
    matrix: NormalizedMatrix,
    operation: OperationSpec,
    result_vector: list[float],
    reference: dict[str, Any],
) -> dict[str, Any]:
    actual = np.asarray(result_vector, dtype=np.float64)
    expected = np.asarray(reference["vector"], dtype=np.float64)
    if actual.shape != expected.shape:
        return {
            "coordinate_basis": "normalized_zero_based_row_column",
            "within_tolerance": False,
            "reason": "result_vector_length_mismatch",
            "actual_length": int(actual.size),
            "expected_length": int(expected.size),
        }
    absolute_error = np.abs(actual - expected)
    max_error = float(np.max(absolute_error)) if absolute_error.size else 0.0
    exceeded_indices = np.flatnonzero(absolute_error > operation.tolerance)
    histogram_upper = max(max_error, operation.tolerance, 1e-15)
    counts, edges = np.histogram(
        absolute_error,
        bins=8,
        range=(0.0, histogram_upper),
    )
    exceeded = [
        {
            "row": int(index),
            "result_index": int(index),
            "normalized_coordinate": "normalized_output_row",
            "expected": float(expected[index]),
            "actual": float(actual[index]),
            "abs_error": float(absolute_error[index]),
        }
        for index in exceeded_indices
    ]
    return {
        "coordinate_basis": "normalized_zero_based_row_column",
        "tolerance": operation.tolerance,
        "max_abs_error": max_error,
        "within_tolerance": bool(exceeded_indices.size == 0),
        "exceeded_count": int(exceeded_indices.size),
        "exceeded_elements": exceeded,
        "error_histogram": {
            "bin_edges": [float(value) for value in edges],
            "counts": [int(value) for value in counts],
            "range": [0.0, histogram_upper],
            "bin_count": 8,
        },
        "elementwise_abs_errors": [float(value) for value in absolute_error],
        "finite": bool(np.all(np.isfinite(actual))),
    }


def compare_vectors(
    left: list[float],
    right: list[float],
    tolerance: float,
) -> dict[str, Any]:
    left_array = np.asarray(left, dtype=np.float64)
    right_array = np.asarray(right, dtype=np.float64)
    if left_array.shape != right_array.shape:
        return {
            "consistent": False,
            "reason": "length_mismatch",
            "left_length": int(left_array.size),
            "right_length": int(right_array.size),
        }
    differences = np.abs(left_array - right_array)
    maximum = float(np.max(differences)) if differences.size else 0.0
    mismatched = np.flatnonzero(differences > tolerance)
    return {
        "consistent": bool(mismatched.size == 0),
        "tolerance": tolerance,
        "max_abs_difference": maximum,
        "mismatched_indices": [int(index) for index in mismatched],
        "mismatched_count": int(mismatched.size),
    }
