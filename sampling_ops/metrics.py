"""Deterministic comparison and resident-memory estimates."""

from __future__ import annotations

from typing import Any

import numpy as np


def ceil_log2(value: int) -> int:
    if value <= 1:
        return 0
    return (value - 1).bit_length()


def effective_chunk_size(vocab_size: int, k_value: int, requested: Any) -> int:
    try:
        chunk = int(requested)
    except (TypeError, ValueError):
        chunk = 4096
    if chunk <= 0:
        chunk = 4096
    # A local stage must be able to hold k candidates.
    chunk = max(chunk, k_value, 1)
    return min(chunk, vocab_size)


def route_estimates(
    vocab_size: int,
    batch_size: int,
    k_value: int,
    dtype_itemsize: int,
    chunk_size: int,
) -> dict[str, dict[str, Any]]:
    """Estimate comparisons and peak resident bytes per route.

    Counts use an explicit, versioned model rather than wall-clock timing so
    route decisions are byte-for-byte reproducible.  Input logits and the
    emitted candidate block are included in every memory estimate; the
    differences therefore come from each route's working set.
    """

    n = int(vocab_size)
    b = int(batch_size)
    k = int(k_value)
    itemsize = int(dtype_itemsize)
    chunk = effective_chunk_size(n, k, chunk_size)
    chunks = (n + chunk - 1) // chunk

    base_memory = b * n * itemsize + b * k * (itemsize + 8)
    output_candidates = b * k * (itemsize + 8)

    full_compare = b * n * ceil_log2(max(n, 2))
    full_memory = base_memory + n * (2 * itemsize + 8)

    heap_sift = ceil_log2(max(k + 1, 2))
    heap_final_sort = ceil_log2(max(k, 2))
    heap_compare = b * (n + max(n - k, 0) * heap_sift + k * heap_final_sort)
    # Python heap entries carry tuple/object overhead; this is a conservative
    # residency model for the explicit heap selector.
    heap_memory = base_memory + k * 48 + 64

    chunk_stage_one = 0
    for chunk_index in range(chunks):
        width = min(chunk, n - chunk_index * chunk)
        chunk_stage_one += width * ceil_log2(max(width, 2))
    merge_candidates = chunks * k
    chunk_merge = merge_candidates * heap_sift + k * heap_final_sort
    chunk_compare = b * (chunk_stage_one + chunk_merge)
    # Streaming local TopK -> global heap: one chunk buffer plus local/global
    # candidate blocks.  This bounds the working set independently of n.
    chunk_memory = base_memory + chunk * (2 * itemsize + 8) + 2 * k * 48 + 128

    common = {
        "comparison_basis": "ceil(log2(n)) comparison model",
        "memory_basis": "input logits + candidate output + route working set",
        "output_candidates_bytes": output_candidates,
    }
    return {
        "full_sort": {
            **common,
            "comparisons": int(full_compare),
            "peak_memory_bytes": int(full_memory),
            "boundary_overhead": 0,
            "chunk_size": None,
            "chunk_count": None,
            "formula": "batch * n * ceil_log2(n)",
        },
        "heap_select": {
            **common,
            "comparisons": int(heap_compare),
            "peak_memory_bytes": int(heap_memory),
            "boundary_overhead": 0,
            "chunk_size": None,
            "chunk_count": None,
            "formula": "batch * (n + heap sift comparisons + final k sort)",
        },
        "chunked_two_stage": {
            **common,
            "comparisons": int(chunk_compare),
            "peak_memory_bytes": int(chunk_memory),
            "boundary_overhead": int(b * chunk_merge),
            "chunk_size": int(chunk),
            "chunk_count": int(chunks),
            "formula": "chunk local sort bound + global merge heap + final k sort",
        },
    }


def dtype_itemsize(dtype: Any) -> int:
    return int(np.dtype(dtype).itemsize)

