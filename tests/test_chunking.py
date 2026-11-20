import numpy as np
import pytest

from opchain.blocks import (
    block_summaries,
    iter_block_ranges,
    merge_welford,
    run_chunked_chain,
    welford_from_values,
)
from opchain.operations import run_reference_chain
from opchain.task import generate_input


def test_block_boundaries_are_explicit_and_complete():
    ranges = list(iter_block_ranges(10, 3))
    assert ranges == [(0, 3), (3, 6), (6, 9), (9, 10)]
    assert ranges[-1][1] == 10


def test_merged_summary_matches_whole_segment():
    rng = np.random.default_rng(0)
    values = rng.standard_normal(128)
    blocks, merged = block_summaries(values, 17, 1e-8)
    whole = welford_from_values(values)
    assert merged["count"] == whole.count
    assert merged["mean"] == pytest.approx(whole.mean, abs=1e-15, rel=1e-15)
    assert merged["variance"] == pytest.approx(
        whole.m2 / whole.count, abs=1e-15, rel=1e-15
    )
    assert blocks[0]["start"] == 0
    assert blocks[-1]["end"] == values.size


def test_block_size_does_not_change_merged_statistics(specs):
    spec = specs[0]
    tensor = generate_input(spec)
    run_a = run_chunked_chain(tensor, spec, 16)
    run_b = run_chunked_chain(tensor, spec, 37)
    for stage_a, stage_b in zip(run_a["stages"], run_b["stages"]):
        for key in ("mean", "variance", "std", "min", "max"):
            assert stage_a["merged_summary"][key] == pytest.approx(
                stage_b["merged_summary"][key], abs=1e-14, rel=1e-14
            )
        assert stage_a["content_hash"] == stage_b["content_hash"]


def test_chunked_matches_reference_and_transpose_is_contiguous(specs):
    for spec in specs:
        tensor = generate_input(spec)
        chunked = run_chunked_chain(tensor, spec, spec.block_size)
        reference_final, reference_stages = run_reference_chain(tensor, spec)
        expected_axes = list(range(tensor.ndim))
        for stage in chunked["stages"]:
            if stage["op"] == "transpose":
                op = spec.op_chain[stage["stage"]]
                expected_axes = [expected_axes[source] for source in op["axes"]]
                assert stage["semantic_axes"] == expected_axes
        for actual, expected in zip(chunked["arrays"], reference_stages):
            actual64 = np.ascontiguousarray(actual, dtype=np.float64)
            if spec.dtype == "float64":
                np.testing.assert_array_equal(actual64, expected["array"])
            else:
                np.testing.assert_allclose(
                    actual64, expected["array"], atol=1e-6, rtol=1e-5
                )
        final64 = np.ascontiguousarray(chunked["final"], dtype=np.float64)
        if spec.dtype == "float64":
            np.testing.assert_array_equal(final64, reference_final)
        else:
            np.testing.assert_allclose(final64, reference_final, atol=1e-6, rtol=1e-5)


def test_welford_merge_order_is_deterministic():
    rng = np.random.default_rng(3)
    values = rng.standard_normal(64)
    left = welford_from_values(values[:32])
    right = welford_from_values(values[32:])
    merged = merge_welford(left, right)
    whole = welford_from_values(values)
    assert merged.mean == pytest.approx(whole.mean, abs=1e-15, rel=1e-15)
    assert merged.m2 == pytest.approx(whole.m2, abs=1e-14, rel=1e-14)
