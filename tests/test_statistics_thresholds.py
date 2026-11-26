from __future__ import annotations

import numpy as np

from telemetry_drift.config import StatisticsConfig, ThresholdConfig
from telemetry_drift.statistics import (
    compute_window_statistics,
    recompute_drift_score,
)
from telemetry_drift.thresholds import compare_threshold, decide_joint


def test_drift_score_recomputes_from_raw_evidence():
    config = StatisticsConfig(
        decay=0.75,
        skew_weight=0.3,
        trend_weight=0.4,
        upstream_weight=0.25,
        residual_scale=0.5,
        input_scale=1.0,
    )
    residuals = [0.1, 0.4, 0.2, 0.9, 1.1, 0.7, 1.3, 0.8]
    deltas = [
        [0.0, 0.0],
        [0.1, -0.1],
        [0.2, 0.1],
        [0.0, 0.4],
        [0.1, 0.2],
        [0.0, -0.2],
        [0.2, 0.1],
        [0.0, 0.0],
    ]
    coefficients = [0.6, -0.3]
    first = compute_window_statistics(
        residuals,
        feature_deltas=deltas,
        coefficients=coefficients,
        config=config,
    )
    second = recompute_drift_score(
        residuals,
        feature_deltas=deltas,
        coefficients=coefficients,
        config=config,
    )
    assert first.drift_score == second
    assert first.variance > 0
    assert first.upstream_contribution > 0


def test_upstream_contribution_changes_score_without_residual_change():
    config = StatisticsConfig(decay=0.9, upstream_weight=0.5, residual_scale=1.0)
    residuals = [0.2] * 8
    quiet = [[0.0, 0.0]] * 8
    disturbed = [[0.0, 0.0], [1.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0], [0.0, 0.0]]
    quiet_stats = compute_window_statistics(
        residuals,
        feature_deltas=quiet,
        coefficients=[1.0, 0.0],
        config=config,
    )
    disturbed_stats = compute_window_statistics(
        residuals,
        feature_deltas=disturbed,
        coefficients=[1.0, 0.0],
        config=config,
    )
    assert disturbed_stats.drift_score > quiet_stats.drift_score


def test_threshold_near_ties_never_flip_between_precisions():
    config = ThresholdConfig(
        policy="strict",
        tolerance=0.0,
        fixed_scale=1_000_000,
        subnormal_floor=1e-15,
    )
    threshold = 1.0
    for value in (
        np.nextafter(threshold, 0.0),
        threshold,
        np.nextafter(threshold, 2.0),
        threshold + 1e-12,
        threshold + 1e-7,
    ):
        result = compare_threshold(value, threshold, "strict", config=config)
        assert len(set(result.precision_decisions.values())) == 1

    near = compare_threshold(1.0 + 1e-12, 1.0, "strict", config=config)
    assert near.exceeds is True
    assert near.reference_fallback is True


def test_tolerance_and_strict_have_explicit_semantics():
    tolerance = ThresholdConfig(
        policy="tolerance", tolerance=1e-6, fixed_scale=1_000_000
    )
    strict = ThresholdConfig(policy="strict", tolerance=1e-6, fixed_scale=1_000_000)
    value = 1.0 + 5e-7
    assert compare_threshold(value, 1.0, "tolerance", config=tolerance).exceeds is False
    assert compare_threshold(value, 1.0, "strict", config=strict).exceeds is True


def test_subnormal_protection_and_joint_rule():
    config = ThresholdConfig(
        policy="strict",
        tolerance=0.0,
        fixed_scale=1_000_000,
        subnormal_floor=1e-12,
        joint_rule="both",
    )
    tiny = compare_threshold(1e-320, 1e-15, "strict", config=config)
    assert tiny.value.subnormal_zeroed is True
    assert tiny.exceeds is False
    tiny_zero_threshold = compare_threshold(
        1e-320, 0.0, "strict", config=config
    )
    assert tiny_zero_threshold.exceeds is False
    decision = decide_joint(
        0.5,
        2.0,
        input_threshold=0.1,
        predictor_threshold=1.0,
        config=config,
    )
    assert decision.drift is True
    assert "JOINT_DRIFT_CONFIRMED" in decision.business_codes

