from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .errors import DomainError, ErrorCode
from .models import Job, SchedulerConfig

INT32_MAX = np.iinfo(np.int32).max
INT32_MIN = np.iinfo(np.int32).min
MAX_CENTERED_INT8 = 255
MAX_PRODUCT = MAX_CENTERED_INT8 * MAX_CENTERED_INT8
DEFAULT_SAFE_SEGMENT_TERMS = INT32_MAX // MAX_PRODUCT


@dataclass(frozen=True)
class TaskArrays:
    input_float: np.ndarray
    weight_float: np.ndarray
    input_q: np.ndarray
    weight_q: np.ndarray
    input_dq: np.ndarray
    weight_dq: np.ndarray


def quantize_array(values: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    if scale <= 0 or not math.isfinite(scale):
        raise DomainError(ErrorCode.E_INVALID_QUANT_SCALE, "quant_scale must be positive")
    if not -128 <= zero_point <= 127:
        raise DomainError(ErrorCode.E_INVALID_ZERO_POINT, "zero_point out of int8 range")
    scaled = np.rint(values.astype(np.float64) / scale + float(zero_point))
    return np.clip(scaled, -128.0, 127.0).astype(np.int8)


def dequantize_array(values: np.ndarray, scale: float, zero_point: int) -> np.ndarray:
    return (values.astype(np.float64) - float(zero_point)) * float(scale)


def generate_arrays(job: Job) -> TaskArrays:
    rng = np.random.default_rng(job.seed)
    input_float = rng.uniform(-1.0, 1.0, size=job.input_shape).astype(np.float64)
    weight_float = rng.uniform(-1.0, 1.0, size=job.kernel_shape).astype(np.float64)
    input_q = quantize_array(input_float, job.quant_scale, job.zero_point)
    weight_q = quantize_array(weight_float, job.quant_scale, job.zero_point)
    return TaskArrays(
        input_float=input_float,
        weight_float=weight_float,
        input_q=input_q,
        weight_q=weight_q,
        input_dq=dequantize_array(input_q, job.quant_scale, job.zero_point),
        weight_dq=dequantize_array(weight_q, job.quant_scale, job.zero_point),
    )


def conv2d_float64(
    inputs: np.ndarray,
    weights: np.ndarray,
    stride: tuple[int, int],
    padding: tuple[int, int],
) -> np.ndarray:
    n, c, h, w = inputs.shape
    k, ck, kh, kw = weights.shape
    if ck != c:
        raise ValueError("input and kernel channel dimensions differ")
    sh, sw = stride
    ph, pw = padding
    out_h = (h + 2 * ph - kh) // sh + 1
    out_w = (w + 2 * pw - kw) // sw + 1
    if out_h <= 0 or out_w <= 0:
        raise ValueError("kernel does not fit the padded input")
    padded = np.pad(
        inputs.astype(np.float64, copy=False),
        ((0, 0), (0, 0), (ph, ph), (pw, pw)),
        mode="constant",
    )
    output = np.zeros((n, k, out_h, out_w), dtype=np.float64)
    for r in range(kh):
        hs = r
        he = hs + (out_h - 1) * sh + 1
        for col in range(kw):
            ws = col
            we = ws + (out_w - 1) * sw + 1
            patch = padded[:, :, hs:he:sh, ws:we:sw]
            output += np.einsum("nchw,kc->nkhw", patch, weights[:, :, r, col])
    return output


def conv2d_int64(
    centered_inputs: np.ndarray,
    centered_weights: np.ndarray,
    stride: tuple[int, int],
    padding: tuple[int, int],
) -> np.ndarray:
    n, c, h, w = centered_inputs.shape
    k, ck, kh, kw = centered_weights.shape
    if ck != c:
        raise ValueError("input and kernel channel dimensions differ")
    sh, sw = stride
    ph, pw = padding
    out_h = (h + 2 * ph - kh) // sh + 1
    out_w = (w + 2 * pw - kw) // sw + 1
    if out_h <= 0 or out_w <= 0:
        raise ValueError("kernel does not fit the padded input")
    padded = np.pad(
        centered_inputs.astype(np.int64, copy=False),
        ((0, 0), (0, 0), (ph, ph), (pw, pw)),
        mode="constant",
    )
    output = np.zeros((n, k, out_h, out_w), dtype=np.int64)
    for r in range(kh):
        hs = r
        he = hs + (out_h - 1) * sh + 1
        for col in range(kw):
            ws = col
            we = ws + (out_w - 1) * sw + 1
            patch = padded[:, :, hs:he:sh, ws:we:sw]
            output += np.einsum(
                "nchw,kc->nkhw", patch, centered_weights[:, :, r, col]
            )
    return output


def reference_tensors(job: Job) -> dict[str, np.ndarray]:
    arrays = generate_arrays(job)
    scale_sq = job.quant_scale * job.quant_scale
    centered_input = arrays.input_q.astype(np.int64) - job.zero_point
    centered_weight = arrays.weight_q.astype(np.int64) - job.zero_point
    reference_float = conv2d_float64(
        arrays.input_float, arrays.weight_float, job.stride, job.padding
    )
    reference_dequant = conv2d_float64(
        arrays.input_dq, arrays.weight_dq, job.stride, job.padding
    )
    direct_int = conv2d_int64(centered_input, centered_weight, job.stride, job.padding)
    direct_scaled = direct_int.astype(np.float64) * scale_sq
    return {
        "input_float": arrays.input_float,
        "weight_float": arrays.weight_float,
        "input_q": arrays.input_q,
        "weight_q": arrays.weight_q,
        "input_dq": arrays.input_dq,
        "weight_dq": arrays.weight_dq,
        "reference_float": reference_float,
        "reference_dequant": reference_dequant,
        "direct_int": direct_int,
        "direct_scaled": direct_scaled,
    }


def segment_terms(job: Job, config: SchedulerConfig) -> int:
    if config.overflow_protection:
        requested = config.max_segment_terms or DEFAULT_SAFE_SEGMENT_TERMS
        return min(requested, DEFAULT_SAFE_SEGMENT_TERMS)
    return config.max_segment_terms or max(1, job.terms_per_output)


def reduction_shape(job: Job, config: SchedulerConfig) -> dict[str, Any]:
    cap = segment_terms(job, config)
    if cap <= 0:
        raise DomainError(
            ErrorCode.E_INVALID_WORKER_CONFIG, "max_segment_terms must be positive"
        )
    terms = job.terms_per_output
    segments = math.ceil(terms / cap)
    return {
        "terms_per_output": terms,
        "max_segment_terms": cap,
        "segments": segments,
        "segmented": segments > 1,
        "worst_case_int32_terms": DEFAULT_SAFE_SEGMENT_TERMS,
        "guard_enabled": config.overflow_protection,
    }


def _validate_chunk(job: Job, chunk: dict[str, Any]) -> None:
    n, k, oh, ow = job.output_shape
    bounds = (
        ("n_start", "n_stop", n),
        ("k_start", "k_stop", k),
        ("oh_start", "oh_stop", oh),
        ("ow_start", "ow_stop", ow),
    )
    for start_name, stop_name, limit in bounds:
        start = chunk.get(start_name)
        stop = chunk.get(stop_name)
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(stop, bool)
            or not isinstance(stop, int)
            or not 0 <= start < stop <= limit
        ):
            raise DomainError(
                ErrorCode.E_INCONSISTENT,
                f"invalid chunk bounds {start_name}={start!r}, {stop_name}={stop!r}",
                job_id=job.job_id,
                chunk_id=chunk.get("chunk_id"),
                stage="chunk_validation",
            )


def compute_chunk(
    job: Job,
    chunk: dict[str, Any],
    config: SchedulerConfig,
    reduction_mode: str = "int64_exact",
) -> tuple[np.ndarray, dict[str, Any]]:
    _validate_chunk(job, chunk)
    arrays = generate_arrays(job)
    centered_input = arrays.input_q.astype(np.int32) - np.int32(job.zero_point)
    centered_weight = arrays.weight_q.astype(np.int32) - np.int32(job.zero_point)

    n0, n1 = int(chunk["n_start"]), int(chunk["n_stop"])
    k0, k1 = int(chunk["k_start"]), int(chunk["k_stop"])
    oh0, oh1 = int(chunk["oh_start"]), int(chunk["oh_stop"])
    ow0, ow1 = int(chunk["ow_start"]), int(chunk["ow_stop"])
    sh, sw = job.stride
    ph, pw = job.padding
    padded = np.pad(
        centered_input,
        ((0, 0), (0, 0), (ph, ph), (pw, pw)),
        mode="constant",
    )
    tile_shape = (n1 - n0, k1 - k0, oh1 - oh0, ow1 - ow0)
    total = np.zeros(tile_shape, dtype=np.int64)
    cap = segment_terms(job, config)
    terms = [
        (channel, row, col)
        for channel in range(job.kernel_shape[1])
        for row in range(job.kernel_shape[2])
        for col in range(job.kernel_shape[3])
    ]
    segment_count = 0
    overflow_positions = 0
    for start in range(0, len(terms), cap):
        segment_count += 1
        segment_int32 = np.zeros(tile_shape, dtype=np.int32)
        segment_exact = (
            None
            if config.overflow_protection
            else np.zeros(tile_shape, dtype=np.int64)
        )
        for channel, row, col in terms[start : start + cap]:
            hs = oh0 * sh + row
            he = (oh1 - 1) * sh + row + 1
            ws = ow0 * sw + col
            we = (ow1 - 1) * sw + col + 1
            patch = padded[n0:n1, channel, hs:he:sh, ws:we:sw]
            kernel_values = centered_weight[k0:k1, channel, row, col]
            exact = patch[:, None, :, :].astype(np.int64) * kernel_values[
                None, :, None, None
            ].astype(np.int64)
            segment_int32 += exact.astype(np.int32)
            if segment_exact is not None:
                segment_exact += exact
                overflow_positions += int(
                    np.count_nonzero(
                        (segment_exact > INT32_MAX) | (segment_exact < INT32_MIN)
                    )
                )
        if segment_exact is not None:
            total += segment_exact
        else:
            total += segment_int32.astype(np.int64)
    if overflow_positions and not config.overflow_protection:
        raise DomainError(
            ErrorCode.E_ACCUMULATOR_OVERFLOW,
            "int32 accumulation overflow detected; overflow protection is disabled",
            job_id=job.job_id,
            chunk_id=int(chunk["chunk_id"]),
            stage="accumulate_reduce",
            detail={
                "overflow_positions": overflow_positions,
                "terms_per_output": job.terms_per_output,
                "max_segment_terms": cap,
            },
        )
    scale_sq = job.quant_scale * job.quant_scale
    output_dtype = np.float32 if reduction_mode == "float32_chunk_reduce" else np.float64
    output = total.astype(np.float64) * scale_sq
    output = output.astype(output_dtype)
    stats = {
        "segment_count": segment_count,
        "int32_overflow_detected": overflow_positions,
        "tile_elements": int(np.prod(tile_shape)),
        "terms_per_output": job.terms_per_output,
        "reduction_mode": reduction_mode,
    }
    return output, stats


def make_chunks(
    job: Job,
    tile_n: int,
    tile_k: int,
    tile_oh: int,
    tile_ow: int,
    loop_order: str,
) -> list[dict[str, Any]]:
    n, k, oh, ow = job.output_shape
    if min(tile_n, tile_k, tile_oh, tile_ow) <= 0:
        raise ValueError("chunk tile dimensions must be positive")
    chunks: list[dict[str, Any]] = []

    def append_chunk(n0: int, n1: int, k0: int, k1: int, oh0: int, oh1: int, ow0: int, ow1: int) -> None:
        chunks.append(
            {
                "job_id": job.job_id,
                "chunk_id": len(chunks),
                "n_start": n0,
                "n_stop": n1,
                "k_start": k0,
                "k_stop": k1,
                "oh_start": oh0,
                "oh_stop": oh1,
                "ow_start": ow0,
                "ow_stop": ow1,
            }
        )

    if loop_order == "n_oh_ow_k":
        for n0 in range(0, n, tile_n):
            for oh0 in range(0, oh, tile_oh):
                for ow0 in range(0, ow, tile_ow):
                    for k0 in range(0, k, tile_k):
                        append_chunk(
                            n0,
                            min(n0 + tile_n, n),
                            k0,
                            min(k0 + tile_k, k),
                            oh0,
                            min(oh0 + tile_oh, oh),
                            ow0,
                            min(ow0 + tile_ow, ow),
                        )
    elif loop_order == "n_k_oh_ow":
        for n0 in range(0, n, tile_n):
            for k0 in range(0, k, tile_k):
                for oh0 in range(0, oh, tile_oh):
                    for ow0 in range(0, ow, tile_ow):
                        append_chunk(
                            n0,
                            min(n0 + tile_n, n),
                            k0,
                            min(k0 + tile_k, k),
                            oh0,
                            min(oh0 + tile_oh, oh),
                            ow0,
                            min(ow0 + tile_ow, ow),
                        )
    else:
        raise ValueError(f"unknown loop order {loop_order!r}")
    return chunks


def coverage_counts(job: Job, chunks: Iterable[dict[str, Any]]) -> dict[str, Any]:
    mask = np.zeros(job.output_shape, dtype=np.int16)
    chunk_ids: set[int] = set()
    for chunk in chunks:
        chunk_id = int(chunk["chunk_id"])
        if chunk_id in chunk_ids:
            raise DomainError(
                ErrorCode.E_INCONSISTENT,
                f"duplicate chunk_id {chunk_id}",
                job_id=job.job_id,
                chunk_id=chunk_id,
            )
        chunk_ids.add(chunk_id)
        _validate_chunk(job, chunk)
        mask[
            int(chunk["n_start"]) : int(chunk["n_stop"]),
            int(chunk["k_start"]) : int(chunk["k_stop"]),
            int(chunk["oh_start"]) : int(chunk["oh_stop"]),
            int(chunk["ow_start"]) : int(chunk["ow_stop"]),
        ] += 1
    total = int(mask.size)
    covered_once = int(np.count_nonzero(mask == 1))
    overlap = int(np.count_nonzero(mask > 1))
    missing = int(np.count_nonzero(mask == 0))
    return {
        "job_id": job.job_id,
        "expected_positions": total,
        "covered_once": covered_once,
        "overlap_positions": overlap,
        "missing_positions": missing,
        "ok": covered_once == total and overlap == 0 and missing == 0,
        "chunk_count": len(chunks),
    }


def merge_chunks(
    job: Job,
    chunk_results: Iterable[tuple[dict[str, Any], np.ndarray]],
    output_dtype: np.dtype[Any] | type[np.float32] | type[np.float64],
) -> np.ndarray:
    output = np.zeros(job.output_shape, dtype=output_dtype)
    seen: set[int] = set()
    for chunk, values in sorted(chunk_results, key=lambda item: int(item[0]["chunk_id"])):
        chunk_id = int(chunk["chunk_id"])
        if chunk_id in seen:
            raise DomainError(
                ErrorCode.E_INCONSISTENT,
                f"chunk {chunk_id} merged more than once",
                job_id=job.job_id,
                chunk_id=chunk_id,
            )
        seen.add(chunk_id)
        expected_shape = (
            int(chunk["n_stop"]) - int(chunk["n_start"]),
            int(chunk["k_stop"]) - int(chunk["k_start"]),
            int(chunk["oh_stop"]) - int(chunk["oh_start"]),
            int(chunk["ow_stop"]) - int(chunk["ow_start"]),
        )
        if tuple(values.shape) != expected_shape:
            raise DomainError(
                ErrorCode.E_INCONSISTENT,
                "chunk result shape does not match its bounds",
                job_id=job.job_id,
                chunk_id=chunk_id,
                stage="merge",
                detail={"expected": expected_shape, "actual": list(values.shape)},
            )
        output[
            int(chunk["n_start"]) : int(chunk["n_stop"]),
            int(chunk["k_start"]) : int(chunk["k_stop"]),
            int(chunk["oh_start"]) : int(chunk["oh_stop"]),
            int(chunk["ow_start"]) : int(chunk["ow_stop"]),
        ] = values
    return output

