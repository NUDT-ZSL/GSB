"""Deterministic chunked execution with block-boundary summary statistics.

Block boundaries are explicit element ranges over the C-contiguous output of
each stage. Welford accumulators are combined across blocks in block-index
order so the merged summary matches the whole-segment statistics exactly (in
float64 arithmetic), independent of block size.
"""

import math
from dataclasses import dataclass

import numpy as np

from .hashing import hash_array
from .operations import activation_forward, transpose_contiguous
from .task import ACTIVATIONS


@dataclass
class Welford:
    count: int
    mean: float
    m2: float
    minimum: float
    maximum: float


def welford_from_values(values):
    flat = np.ascontiguousarray(values, dtype=np.float64).reshape(-1)
    count = int(flat.size)
    if count == 0:
        return Welford(0, 0.0, 0.0, 0.0, 0.0)
    mean = float(np.mean(flat, dtype=np.float64))
    delta = flat - mean
    m2 = float(np.sum(delta * delta, dtype=np.float64))
    return Welford(
        count=count,
        mean=mean,
        m2=m2,
        minimum=float(np.min(flat)),
        maximum=float(np.max(flat)),
    )


def merge_welford(left, right):
    if left.count == 0:
        return right
    if right.count == 0:
        return left
    total = left.count + right.count
    delta = right.mean - left.mean
    mean = left.mean + delta * right.count / total
    m2 = left.m2 + right.m2 + delta * delta * left.count * right.count / total
    return Welford(
        count=total,
        mean=mean,
        m2=m2,
        minimum=min(left.minimum, right.minimum),
        maximum=max(left.maximum, right.maximum),
    )


def summary_payload(welford, eps):
    variance = welford.m2 / welford.count if welford.count else 0.0
    return {
        "count": welford.count,
        "mean": welford.mean,
        "variance": variance,
        "std": math.sqrt(max(variance, 0.0)),
        "min": welford.minimum,
        "max": welford.maximum,
        "eps": float(eps),
    }


def iter_block_ranges(total, block_size):
    start = 0
    while start < total:
        end = min(start + block_size, total)
        yield start, end
        start = end


def block_summaries(flat, block_size, eps):
    records = []
    merged = Welford(0, 0.0, 0.0, 0.0, 0.0)
    for block_index, (start, end) in enumerate(iter_block_ranges(flat.size, block_size)):
        local = welford_from_values(flat[start:end])
        merged = merge_welford(merged, local)
        records.append(
            {
                "block": block_index,
                "start": start,
                "end": end,
                "summary": summary_payload(local, eps),
            }
        )
    return records, summary_payload(merged, eps)


def _stage_record(stage, op, output, block_size, eps, semantic_axes, shape):
    flat = np.ascontiguousarray(output).reshape(-1)
    blocks, merged = block_summaries(flat, block_size, eps)
    return {
        "stage": stage,
        "op": op["op"],
        "shape": list(shape),
        "semantic_axes": list(semantic_axes),
        "content_hash": hash_array(output),
        "block_size": int(block_size),
        "block_count": len(blocks),
        "blocks": blocks,
        "merged_summary": merged,
    }


def run_chunked_chain(input_tensor, spec, block_size, deadline_check=None):
    """Execute the op chain in explicit flat-element blocks.

    Activations are computed block by block on the flat contiguous tensor.
    Transposes permute by the semantic axis order and are re-materialized
    contiguously, then re-blocked over the new flat layout. Returns every
    intermediate stage, block record, summary and the final tensor.
    """
    tensor = np.ascontiguousarray(input_tensor)
    ndim = tensor.ndim
    semantic_axes = list(range(ndim))
    stage_records = []
    stage_arrays = []
    processed_blocks = 0

    input_flat = tensor.reshape(-1)
    input_blocks, input_summary = block_summaries(input_flat, block_size, spec.eps)
    input_record = {
        "stage": -1,
        "op": "input",
        "shape": list(tensor.shape),
        "semantic_axes": list(semantic_axes),
        "content_hash": hash_array(tensor),
        "block_size": int(block_size),
        "block_count": len(input_blocks),
        "blocks": input_blocks,
        "merged_summary": input_summary,
    }

    for stage, op in enumerate(spec.op_chain):
        if deadline_check is not None:
            deadline_check(stage, processed_blocks)
        name = op["op"]
        if name in ACTIVATIONS:
            kind = op.get("kind", spec.activation_kind)
            flat = tensor.reshape(-1)
            output = np.empty_like(flat)
            for start, end in iter_block_ranges(flat.size, block_size):
                output[start:end] = activation_forward(kind, flat[start:end])
                processed_blocks += 1
                if deadline_check is not None:
                    deadline_check(stage, processed_blocks)
            tensor = output.reshape(tensor.shape)
        else:  # transpose: reorder by semantic axes, then make contiguous
            axes = op["axes"]
            tensor = transpose_contiguous(tensor, axes)
            semantic_axes = [semantic_axes[source] for source in axes]
            flat = tensor.reshape(-1)
            # Explicit output-flat blocks make the contiguity boundary auditable.
            for _ in iter_block_ranges(flat.size, block_size):
                processed_blocks += 1
                if deadline_check is not None:
                    deadline_check(stage, processed_blocks)

        record = _stage_record(
            stage, op, tensor, block_size, spec.eps, semantic_axes, tensor.shape
        )
        stage_records.append(record)
        stage_arrays.append(np.ascontiguousarray(tensor))

    final_tensor = np.ascontiguousarray(tensor)
    return {
        "input_record": input_record,
        "stages": stage_records,
        "arrays": stage_arrays,
        "final": final_tensor,
        "final_hash": hash_array(final_tensor),
        "processed_blocks": processed_blocks,
        "block_size": int(block_size),
    }
