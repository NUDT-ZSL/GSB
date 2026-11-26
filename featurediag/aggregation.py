"""Exact blockwise pairwise feature-cross aggregation and mismatch diagnosis."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import BusinessError
from .models import Task


@dataclass(frozen=True)
class PairSpec:
    output_index: int
    block_index: int
    local_index: int
    feature_i: int
    feature_j: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_index": self.output_index,
            "block_index": self.block_index,
            "local_index": self.local_index,
            "feature_i": self.feature_i,
            "feature_j": self.feature_j,
        }


def pair_specs(task: Task) -> list[PairSpec]:
    """Return all unordered in-block pairs, including diagonal pairs.

    A block owns its output pairs exclusively. Pair products are summed over all
    rows with an exact expansion (``math.fsum``), so computing blocks separately
    and concatenating them in declared block order cannot change a float bit.
    """

    specs: list[PairSpec] = []
    for block_index, (start, stop) in enumerate(task.block_plan):
        local_index = 0
        for feature_i in range(start, stop):
            for feature_j in range(feature_i, stop):
                specs.append(
                    PairSpec(
                        output_index=len(specs),
                        block_index=block_index,
                        local_index=local_index,
                        feature_i=feature_i,
                        feature_j=feature_j,
                    )
                )
                local_index += 1
    return specs


def _pair_sum(column_i: np.ndarray, column_j: np.ndarray) -> float:
    products = np.asarray(column_i * column_j, dtype=np.float64)
    return math.fsum(float(value) for value in products)


def aggregate_in_blocks(normalized: np.ndarray, task: Task) -> tuple[np.ndarray, list[dict[str, Any]]]:
    matrix = np.asarray(normalized, dtype=np.float64)
    if matrix.ndim != 2 or matrix.shape[1] != task.feature_dim:
        raise BusinessError(
            "E_BLOCK_MISMATCH",
            "aggregation input dimension does not match task",
            stage="aggregate",
            task_id=task.task_id,
            context={"shape": list(matrix.shape), "feature_dim": task.feature_dim},
        )
    specs = pair_specs(task)
    block_arrays: list[np.ndarray] = []
    block_metadata: list[dict[str, Any]] = []
    output_cursor = 0
    for block_index, (start, stop) in enumerate(task.block_plan):
        local_specs = [spec for spec in specs if spec.block_index == block_index]
        values = [
            _pair_sum(matrix[:, spec.feature_i], matrix[:, spec.feature_j])
            for spec in local_specs
        ]
        block_values = np.asarray(values, dtype=np.float64)
        block_arrays.append(block_values)
        block_metadata.append(
            {
                "block_index": block_index,
                "feature_start": start,
                "feature_stop": stop,
                "output_start": output_cursor,
                "output_stop": output_cursor + len(local_specs),
                "pair_count": len(local_specs),
            }
        )
        output_cursor += len(local_specs)
    reduced = reduce_block_outputs(block_arrays, task.reduction_rule)
    return reduced, block_metadata


def aggregate_monolithic(normalized: np.ndarray, task: Task) -> np.ndarray:
    """One-pass float64 reference over the same canonical pair set."""

    matrix = np.asarray(normalized, dtype=np.float64)
    specs = pair_specs(task)
    values = [_pair_sum(matrix[:, spec.feature_i], matrix[:, spec.feature_j]) for spec in specs]
    return np.asarray(values, dtype=np.float64)


def reduce_block_outputs(
    block_arrays: list[np.ndarray], reduction_rule: str
) -> np.ndarray:
    if reduction_rule not in {
        "canonical_block_order_exact",
        "ordered_block_concat_exact",
        "exact_block_order",
        "block_order",
    }:
        raise BusinessError(
            "E_PARAM_INVALID",
            "unsupported cross-block reduction rule",
            stage="aggregate",
            context={"reduction_rule": reduction_rule},
        )
    if not block_arrays:
        return np.empty((0,), dtype=np.float64)
    pieces = [np.asarray(part, dtype=np.float64) for part in block_arrays]
    return np.ascontiguousarray(np.concatenate(pieces), dtype=np.float64)


def compare_block_results(
    blockwise: np.ndarray,
    monolithic: np.ndarray,
    task: Task,
    *,
    batch_id: str,
) -> dict[str, Any]:
    blockwise_array = np.asarray(blockwise, dtype=np.float64)
    monolithic_array = np.asarray(monolithic, dtype=np.float64)
    if blockwise_array.shape != monolithic_array.shape:
        length = min(blockwise_array.size, monolithic_array.size)
        mismatch_index = int(np.flatnonzero(blockwise_array[:length] != monolithic_array[:length])[0]) if length and np.any(blockwise_array[:length] != monolithic_array[:length]) else length
        specs = pair_specs(task)
        pair = specs[mismatch_index] if mismatch_index < len(specs) else None
        raise BusinessError(
            "E_BLOCK_MISMATCH",
            "blockwise and monolithic output shapes differ",
            stage="aggregate",
            task_id=task.task_id,
            batch_id=batch_id,
            context={
                "block_shape": list(blockwise_array.shape),
                "monolithic_shape": list(monolithic_array.shape),
                "first_mismatch_position": mismatch_index,
                "feature_pair": [pair.feature_i, pair.feature_j] if pair else None,
                "block_index": pair.block_index if pair else None,
                "pair_local_index": pair.local_index if pair else None,
            },
        )
    unequal = blockwise_array != monolithic_array
    unequal |= np.isnan(blockwise_array) != np.isnan(monolithic_array)
    if not np.any(unequal):
        return {
            "equal": True,
            "element_count": int(blockwise_array.size),
            "first_mismatch_position": None,
            "feature_pair": None,
        }
    first_index = int(np.flatnonzero(unequal)[0])
    specs = pair_specs(task)
    pair = specs[first_index]
    raise BusinessError(
        "E_BLOCK_MISMATCH",
        "blockwise aggregation differs from monolithic float64 aggregation",
        stage="aggregate",
        task_id=task.task_id,
        batch_id=batch_id,
        context={
            "first_mismatch_position": first_index,
            "feature_pair": [pair.feature_i, pair.feature_j],
            "block_index": pair.block_index,
            "pair_local_index": pair.local_index,
            "output_index": pair.output_index,
            "blockwise_value": float(blockwise_array[first_index]),
            "monolithic_value": float(monolithic_array[first_index]),
        },
    )
