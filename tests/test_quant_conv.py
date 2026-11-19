from __future__ import annotations

import numpy as np
import pytest

from qconv import quant_conv
from qconv.errors import DomainError, ErrorCode
from qconv.models import Job, SchedulerConfig
from qconv.quant_conv import (
    compute_chunk,
    conv2d_float64,
    conv2d_int64,
    coverage_counts,
    dequantize_array,
    generate_arrays,
    make_chunks,
    merge_chunks,
    quantize_array,
    reference_tensors,
)


def test_seeded_arrays_are_reproducible_and_quantization_is_exact(
    small_job: Job,
) -> None:
    first = generate_arrays(small_job)
    second = generate_arrays(small_job)
    assert np.array_equal(first.input_float, second.input_float)
    assert np.array_equal(first.weight_float, second.weight_float)
    assert np.array_equal(first.input_q, second.input_q)
    assert first.input_q.dtype == np.int8
    assert first.weight_q.dtype == np.int8
    assert int(first.input_q.min()) >= -128
    assert int(first.input_q.max()) <= 127
    expected = dequantize_array(
        first.input_q, small_job.quant_scale, small_job.zero_point
    )
    assert np.array_equal(first.input_dq, expected)
    round_trip = quantize_array(expected, small_job.quant_scale, small_job.zero_point)
    assert np.array_equal(round_trip, first.input_q)


def test_float_and_int_reference_agree_for_dequantized_values(
    small_job: Job,
) -> None:
    tensors = reference_tensors(small_job)
    float_reference = conv2d_float64(
        tensors["input_dq"], tensors["weight_dq"], small_job.stride, small_job.padding
    )
    direct = conv2d_int64(
        tensors["input_q"].astype(np.int64) - small_job.zero_point,
        tensors["weight_q"].astype(np.int64) - small_job.zero_point,
        small_job.stride,
        small_job.padding,
    )
    scaled = direct.astype(np.float64) * small_job.quant_scale**2
    assert np.allclose(scaled, float_reference, atol=1e-12, rtol=0)
    assert np.array_equal(direct, tensors["direct_int"])


def test_tiled_chunks_cover_each_output_once_and_merge_exactly(
    small_job: Job,
) -> None:
    config = SchedulerConfig(overflow_protection=True, max_segment_terms=4)
    chunks = make_chunks(small_job, 1, 2, 3, 2, "n_oh_ow_k")
    coverage = coverage_counts(small_job, chunks)
    assert coverage["ok"]
    assert coverage["overlap_positions"] == 0
    assert coverage["missing_positions"] == 0
    results = []
    stats = []
    for chunk in chunks:
        values, chunk_stats = compute_chunk(small_job, chunk, config)
        results.append((chunk, values))
        stats.append(chunk_stats)
    merged = merge_chunks(small_job, results, np.float64)
    reference = reference_tensors(small_job)["direct_scaled"]
    assert np.allclose(merged, reference, atol=1e-12, rtol=0)
    assert all(item["segment_count"] > 1 for item in stats)
    assert all(item["int32_overflow_detected"] == 0 for item in stats)


def test_overflow_detection_returns_structured_business_error(
    small_job: Job, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(quant_conv, "INT32_MAX", 10)
    monkeypatch.setattr(quant_conv, "INT32_MIN", -10)
    config = SchedulerConfig(
        overflow_protection=False,
        max_segment_terms=max(1, small_job.terms_per_output),
    )
    chunk = make_chunks(small_job, 1, 3, 6, 6, "n_k_oh_ow")[0]
    with pytest.raises(DomainError) as exc_info:
        compute_chunk(small_job, chunk, config)
    assert exc_info.value.code == ErrorCode.E_ACCUMULATOR_OVERFLOW
    assert exc_info.value.stage == "accumulate_reduce"
    assert exc_info.value.job_id == small_job.job_id
    assert exc_info.value.chunk_id == 0


def test_coverage_detects_overlap(small_job: Job) -> None:
    chunks = make_chunks(small_job, 1, 3, 6, 6, "n_k_oh_ow")
    duplicate = dict(chunks[0])
    duplicate["chunk_id"] = 99
    result = coverage_counts(small_job, chunks + [duplicate])
    assert not result["ok"]
    assert result["overlap_positions"] > 0

