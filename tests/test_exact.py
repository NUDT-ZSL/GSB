from __future__ import annotations

import math

import numpy as np

from fieldengine.exact import (
    add_bins,
    exact_reduce,
    sum_float_from_bins,
    variance_float_from_bins,
)


def _sums(values: np.ndarray, groups: np.ndarray) -> list[float]:
    n_groups = int(groups.max()) + 1 if groups.size else 0
    reduced = exact_reduce(values, groups, n_groups)
    return [
        sum_float_from_bins(
            reduced["sum_bins"][index],
            {key: value[index] for key, value in reduced["anomalies"].items()},
        )
        for index in range(n_groups)
    ]


def test_exact_sum_is_independent_of_partition_boundaries() -> None:
    rng = np.random.default_rng(7)
    values = rng.normal(size=257)
    groups = np.zeros(values.size, dtype=np.int64)
    full_state = exact_reduce(values, groups, 1)
    full_bins = full_state["sum_bins"][0]
    merged_bins: dict[int, int] = {}
    for start, stop in [(0, 1), (1, 19), (19, 100), (100, 257)]:
        state = exact_reduce(
            values[start:stop],
            np.zeros(stop - start, dtype=np.int64),
            1,
        )
        add_bins(merged_bins, state["sum_bins"][0])
    anomaly = {key: 0 for key in full_state["anomalies"]}
    full = sum_float_from_bins(full_bins, anomaly)
    merged = sum_float_from_bins(merged_bins, anomaly)
    assert merged.hex() == full.hex()


def test_exact_mean_and_variance_are_correctly_rounded() -> None:
    values = np.asarray([1.0, 2.0, 3.0, 4.0], dtype=np.float64)
    groups = np.zeros(4, dtype=np.int64)
    reduced = exact_reduce(values, groups, 1)
    anomaly = {key: item[0] for key, item in reduced["anomalies"].items()}
    assert sum_float_from_bins(reduced["sum_bins"][0], anomaly).hex() == (10.0).hex()
    assert (
        variance_float_from_bins(
            reduced["sum_bins"][0], reduced["square_bins"][0], anomaly, 4
        ).hex()
        == (1.25).hex()
    )


def test_nonfinite_values_are_counted_and_propagated() -> None:
    values = np.asarray([1.0, math.nan, math.inf, -math.inf], dtype=np.float64)
    groups = np.asarray([0, 0, 0, 1], dtype=np.int64)
    reduced = exact_reduce(values, groups, 2)
    assert reduced["counts"] == [3, 1]
    assert reduced["anomalies"]["nan"] == [1, 0]
    assert reduced["anomalies"]["posinf"] == [1, 0]
    assert reduced["anomalies"]["neginf"] == [0, 1]
    assert math.isnan(
        sum_float_from_bins(
            reduced["sum_bins"][0],
            {key: item[0] for key, item in reduced["anomalies"].items()},
        )
    )
    assert math.isinf(
        sum_float_from_bins(
            reduced["sum_bins"][1],
            {key: item[1] for key, item in reduced["anomalies"].items()},
        )
    )
