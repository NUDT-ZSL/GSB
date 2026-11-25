from __future__ import annotations

import math
from typing import Any, Callable

import numpy as np

from .canonical import array_digest
from .errors import Codes, WorkbenchError


LOOP_ORDER = ["batch", "m_block", "n_block", "k_block", "k_inner"]
ACCUMULATION_ORDER = "increasing_k_then_neumaier_compensation"


def _float64_inputs(spec: dict[str, Any], arrays: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a = np.ascontiguousarray(arrays["A"], dtype=np.float64)
    b = np.ascontiguousarray(arrays["B"], dtype=np.float64)
    c_array = arrays.get("C")
    if c_array is None:
        c = np.zeros(spec["matrix_shapes"]["C"], dtype=np.float64)
    else:
        c = np.ascontiguousarray(c_array, dtype=np.float64)
    return a, b, c


def _dimensions(spec: dict[str, Any]) -> tuple[int, int, int, int]:
    shapes = spec["matrix_shapes"]
    if spec["op_kind"] == "gemm":
        m, k = shapes["A"]
        _, n = shapes["B"]
        return 1, int(m), int(n), int(k)
    batch, m, k = shapes["A"]
    _, _, n = shapes["B"]
    return int(batch), int(m), int(n), int(k)


def tiled_multiply_accumulate(
    spec: dict[str, Any],
    arrays: dict[str, np.ndarray],
    *,
    block_size: int | None = None,
    on_chunk: Callable[[str, np.ndarray, dict[str, Any]], dict[str, Any] | None] | None = None,
) -> dict[str, Any]:
    """Compute alpha * A @ B + beta * C with deterministic tiled K accumulation."""

    requested_block = int(spec["block_size"] if block_size is None else block_size)
    if requested_block <= 0:
        raise WorkbenchError(Codes.BLOCK_INVALID, "effective block_size must be positive")

    a, b, c = _float64_inputs(spec, arrays)
    batch, m_dim, n_dim, k_dim = _dimensions(spec)
    alpha = np.float64(spec["alpha"])
    beta = np.float64(spec["beta"])
    output = np.zeros((batch, m_dim, n_dim), dtype=np.float64)
    chunks: list[dict[str, Any]] = []
    subnormal_count = 0
    tiny = np.finfo(np.float64).tiny

    for batch_index in range(batch):
        a_batch = a if spec["op_kind"] == "gemm" else a[batch_index]
        b_batch = b if spec["op_kind"] == "gemm" else b[batch_index]
        c_batch = c if spec["op_kind"] == "gemm" else c[batch_index]
        out_batch = output[0] if spec["op_kind"] == "gemm" else output[batch_index]

        for i0 in range(0, m_dim, requested_block):
            i1 = min(i0 + requested_block, m_dim)
            for j0 in range(0, n_dim, requested_block):
                j1 = min(j0 + requested_block, n_dim)
                total = np.array(beta * c_batch[i0:i1, j0:j1], dtype=np.float64, copy=True)
                correction = np.zeros_like(total)
                subnormal_guard = np.zeros_like(total)

                for k0 in range(0, k_dim, requested_block):
                    k1 = min(k0 + requested_block, k_dim)
                    for kk in range(k0, k1):
                        a_column = a_batch[i0:i1, kk]
                        b_row = b_batch[kk, j0:j1]
                        term = (a_column[:, None] * b_row[None, :]) * alpha

                        subnormal_mask = (term != 0.0) & (np.abs(term) < tiny)
                        if bool(subnormal_mask.any()):
                            positions = np.argwhere(subnormal_mask)
                            for pos in positions:
                                row, col = int(pos[0]), int(pos[1])
                                flat_index = row * term.shape[1] + col
                                guarded = math.fsum(
                                    [float(subnormal_guard.flat[flat_index]), float(term[row, col])]
                                )
                                subnormal_guard.flat[flat_index] = guarded
                                subnormal_count += 1
                            normal_term = np.where(subnormal_mask, 0.0, term)
                        else:
                            normal_term = term

                        updated = total + normal_term
                        correction += np.where(
                            np.abs(total) >= np.abs(normal_term),
                            (total - updated) + normal_term,
                            (normal_term - updated) + total,
                        )
                        total = updated

                tile = (total + correction) + subnormal_guard
                out_batch[i0:i1, j0:j1] = tile
                chunk_id = f"{spec['job_id']}:b{batch_index}:i{i0}:j{j0}"
                chunk_info: dict[str, Any] = {
                    "chunk_id": chunk_id,
                    "job_id": spec["job_id"],
                    "batch_index": batch_index,
                    "m_start": i0,
                    "m_end": i1,
                    "n_start": j0,
                    "n_end": j1,
                    "shape": [int(i1 - i0), int(j1 - j0)],
                    "digest": array_digest(tile)["sha256"],
                    "bytes": int(tile.nbytes),
                    "accumulation": ACCUMULATION_ORDER,
                }
                if on_chunk is not None:
                    extra = on_chunk(chunk_id, np.array(tile, dtype=np.float64, copy=True), chunk_info)
                    if isinstance(extra, dict):
                        chunk_info.update(extra)
                chunks.append(chunk_info)

    return {
        "output": output,
        "chunks": chunks,
        "loop_order": LOOP_ORDER,
        "accumulation_order": ACCUMULATION_ORDER,
        "subnormal_terms_protected": int(subnormal_count),
        "block_size": int(requested_block),
    }


def reference_multiply(spec: dict[str, Any], arrays: dict[str, np.ndarray]) -> np.ndarray:
    a, b, c = _float64_inputs(spec, arrays)
    if spec["op_kind"] == "gemm":
        product = np.matmul(a, b)
        result = spec["alpha"] * product + spec["beta"] * c
        return result[None, :, :]
    product = np.matmul(a, b)
    return spec["alpha"] * product + spec["beta"] * c


def deviation_spectrum(actual: np.ndarray, reference: np.ndarray) -> dict[str, Any]:
    actual64 = np.asarray(actual, dtype=np.float64)
    reference64 = np.asarray(reference, dtype=np.float64)
    difference = np.abs(actual64 - reference64)
    histogram, edges = np.histogram(difference, bins=12)
    flat_differences = difference.reshape(-1)
    return {
        "max_abs_err": float(difference.max(initial=0.0)),
        "mean_abs_err": float(difference.mean() if difference.size else 0.0),
        "p50_abs_err": float(np.percentile(flat_differences, 50)),
        "p95_abs_err": float(np.percentile(flat_differences, 95)),
        "p99_abs_err": float(np.percentile(flat_differences, 99)),
        "histogram_counts": [int(value) for value in histogram],
        "histogram_edges": [float(value) for value in edges],
        "element_count": int(difference.size),
    }


def verify_against_reference(
    spec: dict[str, Any],
    actual: np.ndarray,
    arrays: dict[str, np.ndarray],
) -> dict[str, Any]:
    reference = reference_multiply(spec, arrays)
    actual64 = np.asarray(actual, dtype=np.float64)
    tolerance = spec["tolerance"]
    rtol = float(tolerance["rtol"])
    atol = float(tolerance["atol"])
    threshold = atol + rtol * np.abs(reference)
    outside = np.abs(actual64 - reference) > threshold
    spectrum = deviation_spectrum(actual64, reference)
    spectrum["outside_tolerance_count"] = int(outside.sum())
    spectrum["rtol"] = rtol
    spectrum["atol"] = atol

    if bool(outside.any()):
        flat_index = int(np.argmax(outside))
        index = np.unravel_index(flat_index, outside.shape)
        index_list = [int(value) for value in index]
        raise WorkbenchError(
            Codes.TOLERANCE_EXCEEDED,
            "tiled result exceeds float64 reference tolerance",
            {
                "job_id": spec["job_id"],
                "first_deviation": {
                    "index": index_list,
                    "flat_index": flat_index,
                    "actual": float(actual64[index]),
                    "reference": float(reference[index]),
                    "abs_err": float(abs(actual64[index] - reference[index])),
                    "allowed": float(threshold[index]),
                },
                "spectrum": spectrum,
            },
        )
    return spectrum


def canonical_output_digest(output: np.ndarray) -> str:
    from .canonical import array_digest

    return array_digest(np.asarray(output, dtype=np.float64))["sha256"]
