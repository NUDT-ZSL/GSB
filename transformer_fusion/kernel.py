"""Two-stage blocked fused kernel and the per-operator reference path."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np

from .model import TaskCase
from .ops import activation, apply_chain
from .util import canonical_json, digest_array


def block_ranges(numel: int, block_size: int) -> list[tuple[int, int]]:
    if block_size <= 0:
        raise ValueError("block_size must be positive")
    return [(start, min(start + block_size, numel)) for start in range(0, numel, block_size)]


def block_target_indices(
    input_shape: Sequence[int],
    axis_order: Sequence[int],
    start: int,
    stop: int,
) -> np.ndarray:
    """Map a flat input block to unique flat output locations."""

    shape = tuple(int(x) for x in input_shape)
    axes = tuple(int(x) for x in axis_order)
    output_shape = tuple(shape[axis] for axis in axes)
    flat = np.arange(start, stop, dtype=np.int64)
    coordinates = np.unravel_index(flat, shape)
    target_coordinates = tuple(coordinates[axis] for axis in axes)
    return np.ravel_multi_index(target_coordinates, output_shape).astype(np.int64, copy=False)


def _intermediate_hasher(activation_shape: Sequence[int]):
    digest = hashlib.sha256()
    header = canonical_json(
        {
            "domain": "tf-activation-intermediate-v1",
            "dtype": "float64",
            "shape": [int(x) for x in activation_shape],
            "order": "C",
        }
    )
    digest.update(header.encode("utf-8"))
    digest.update(b"\0")
    return digest


@dataclass
class KernelResult:
    output: np.ndarray
    intermediate_digest: str | None
    block_boundaries: list[tuple[int, int]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def _prefix_and_activation_shape(
    base_input: np.ndarray,
    task: TaskCase,
    activation_index: int,
) -> tuple[np.ndarray, tuple[int, ...]]:
    prefix = tuple(task.op_chain[:activation_index])
    if prefix:
        prefix_output = apply_chain(base_input, prefix, task.axis_order, task.eps)
    else:
        prefix_output = base_input
    return prefix_output, tuple(prefix_output.shape)


def execute_separate(
    base_input: np.ndarray,
    task: TaskCase,
    *,
    activation_index: int,
    transpose_index: int,
    block_size: int | None = None,
) -> KernelResult:
    """Execute activation and transpose as two distinct operators.

    The activation materializes a full intermediate array.  It is never written
    to the cache or disk by this component.
    """

    arrays = np.asarray(base_input, dtype=np.float64)
    prefix_output, activation_shape = _prefix_and_activation_shape(arrays, task, activation_index)
    effective_block = int(block_size or task.block_size)
    boundaries = block_ranges(int(prefix_output.size), effective_block)
    intermediate = np.empty(activation_shape, dtype=np.float64)
    flat_input = prefix_output.reshape(-1)
    flat_intermediate = intermediate.reshape(-1)
    hasher = _intermediate_hasher(activation_shape)
    for start, stop in boundaries:
        block = activation(task.op_chain[activation_index], flat_input[start:stop], task.eps)
        flat_intermediate[start:stop] = block
        hasher.update(np.ascontiguousarray(block).tobytes(order="C"))
    transposed = np.transpose(intermediate, axes=task.axis_order)
    suffix = tuple(task.op_chain[transpose_index + 1 :])
    output = apply_chain(transposed, suffix, task.axis_order, task.eps) if suffix else transposed
    return KernelResult(
        output=np.ascontiguousarray(output, dtype=np.float64),
        intermediate_digest=hasher.hexdigest(),
        block_boundaries=boundaries,
        metadata={
            "path": "separate",
            "materialized_intermediate_bytes": int(intermediate.nbytes),
            "block_count": len(boundaries),
        },
    )


def execute_fused(
    base_input: np.ndarray,
    task: TaskCase,
    *,
    activation_index: int,
    transpose_index: int,
    block_size: int | None = None,
) -> KernelResult:
    """Fuse activation and transpose in one blocked kernel.

    Each input block is activated in registers/local arrays and immediately
    scattered to output coordinates.  No activation-sized intermediate array is
    allocated.
    """

    arrays = np.asarray(base_input, dtype=np.float64)
    prefix_output, activation_shape = _prefix_and_activation_shape(arrays, task, activation_index)
    effective_block = int(block_size or task.block_size)
    boundaries = block_ranges(int(prefix_output.size), effective_block)
    output_shape = tuple(activation_shape[axis] for axis in task.axis_order)
    output_flat = np.empty(int(np.prod(output_shape, dtype=np.int64)), dtype=np.float64)
    flat_input = prefix_output.reshape(-1)
    hasher = _intermediate_hasher(activation_shape)
    for start, stop in boundaries:
        block = activation(task.op_chain[activation_index], flat_input[start:stop], task.eps)
        hasher.update(np.ascontiguousarray(block).tobytes(order="C"))
        targets = block_target_indices(activation_shape, task.axis_order, start, stop)
        output_flat[targets] = block
    output = output_flat.reshape(output_shape)
    suffix = tuple(task.op_chain[transpose_index + 1 :])
    if suffix:
        output = apply_chain(output, suffix, task.axis_order, task.eps)
    return KernelResult(
        output=np.ascontiguousarray(output, dtype=np.float64),
        intermediate_digest=hasher.hexdigest(),
        block_boundaries=boundaries,
        metadata={
            "path": "fused",
            "materialized_intermediate_bytes": 0,
            "block_count": len(boundaries),
            "output_shape": list(output_shape),
        },
    )


def execute_chain_only(base_input: np.ndarray, task: TaskCase) -> KernelResult:
    arrays = np.asarray(base_input, dtype=np.float64)
    output = apply_chain(arrays, task.op_chain, task.axis_order, task.eps)
    return KernelResult(
        output=np.ascontiguousarray(output, dtype=np.float64),
        intermediate_digest=None,
        block_boundaries=[],
        metadata={"path": "separate_chain", "materialized_intermediate_bytes": 0},
    )


def output_digest(array: np.ndarray, domain: str) -> str:
    return digest_array(array, domain=domain)


def independent_fused_recompute(
    base_input: np.ndarray,
    task: TaskCase,
    activation_index: int,
    transpose_index: int,
) -> np.ndarray:
    """Independent reference built from activation followed by explicit reorder."""

    arrays = np.asarray(base_input, dtype=np.float64)
    prefix = tuple(task.op_chain[:activation_index])
    prefix_output = apply_chain(arrays, prefix, task.axis_order, task.eps) if prefix else arrays
    activated = activation(task.op_chain[activation_index], prefix_output, task.eps)
    reordered = np.transpose(activated, axes=task.axis_order)
    suffix = tuple(task.op_chain[transpose_index + 1 :])
    output = apply_chain(reordered, suffix, task.axis_order, task.eps) if suffix else reordered
    return np.ascontiguousarray(output, dtype=np.float64)
