from __future__ import annotations

from dataclasses import replace

import pytest

from driftmon.canonical import canonical_bytes
from driftmon.config import default_config
from driftmon.data import generate_stream
from driftmon.errors import E_CONFIG_INVALID, AppError
from driftmon.models import ResourceLimits
from driftmon.stats import FeatureAccumulator, build_feature_snapshot, compute_statistics


def test_non_positive_window_returns_config_error() -> None:
    with pytest.raises(AppError) as captured:
        replace(default_config(), window_size=0)
    assert captured.value.code == E_CONFIG_INVALID
    assert captured.value.field == "window_size"


def test_slide_step_outside_window_returns_config_error() -> None:
    with pytest.raises(AppError) as captured:
        replace(default_config(), window_size=2, slide_step=3)
    assert captured.value.code == E_CONFIG_INVALID
    assert captured.value.field == "slide_step"


def test_window_stats_exactly_equal_direct_window_slice() -> None:
    config = default_config()
    batches = generate_stream(config)
    feature = config.feature("transaction_amount")
    snapshot = build_feature_snapshot(config, batches, feature, 4)
    direct_values = []
    for batch_index in range(2, 5):
        direct_values.extend(batches[batch_index].column(feature.name).values)
    direct = compute_statistics(feature, direct_values, config.quantiles)
    assert canonical_bytes(snapshot["statistics"]["monitor_window"]) == canonical_bytes(
        direct
    )
    assert snapshot["windows"]["monitor"]["start_batch"] == 2
    assert snapshot["windows"]["monitor"]["end_batch"] == 4
    assert snapshot["windows"]["monitor"]["removed_batch_count"] == 2
    assert snapshot["windows"]["monitor"]["removed_sample_count"] == 128
    assert snapshot["windows"]["full"]["sample_count"] == 320
    assert snapshot["statistics"]["full"]["valid_count"] == 320
    assert snapshot["statistics"]["monitor_window"]["valid_count"] == 192


def test_window_removal_is_not_incremental_approximation() -> None:
    config = default_config()
    feature = config.feature("region")
    batches = generate_stream(config)
    first = build_feature_snapshot(config, batches, feature, 4)
    shorter = replace(config, window_size=2)
    second = build_feature_snapshot(shorter, batches, feature, 4)
    direct_values = []
    for batch_index in range(3, 5):
        direct_values.extend(batches[batch_index].column(feature.name).values)
    direct = compute_statistics(feature, direct_values, shorter.quantiles)
    assert canonical_bytes(second["statistics"]["monitor_window"]) == canonical_bytes(
        direct
    )
    assert second["windows"]["monitor"]["removed_sample_count"] == 192
    assert first["windows"]["monitor"]["removed_sample_count"] == 128


def test_chunked_accumulator_exact_remove() -> None:
    config = default_config()
    batches = generate_stream(config)
    feature = config.feature("session_minutes")
    accumulator = FeatureAccumulator(feature, config.quantiles)
    for index in range(0, 3):
        accumulator.add_batch(batches[index])
    combined = accumulator.statistics()
    assert combined["valid_count"] == 192
    assert accumulator.remove_batch(0) is True
    assert accumulator.batch_indices() == [1, 2]
    direct_values = []
    for index in (1, 2):
        direct_values.extend(batches[index].column(feature.name).values)
    direct = compute_statistics(feature, direct_values, config.quantiles)
    assert canonical_bytes(accumulator.statistics()) == canonical_bytes(direct)

