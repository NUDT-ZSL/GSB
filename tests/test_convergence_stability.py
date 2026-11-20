"""Requirement 6: threshold strategies and subnormal protection."""

from __future__ import annotations

import numpy as np

from seraph_core.config.config import EngineConfig
from seraph_core.engine.training import LinearRegressor
from seraph_core.engine.precision import flush_subnormals


def test_tolerance_band_absorbs_boundary_rounding():
    strict = EngineConfig(convergence_strategy="strict",
                          convergence_tolerance=1e-7, tolerance_band=0.0)
    banded = EngineConfig(convergence_strategy="tolerance_band",
                          convergence_tolerance=1e-7,
                          tolerance_band=1e-9)
    model_strict = LinearRegressor(3, strict)
    model_banded = LinearRegressor(3, banded)
    # Improvement sitting inside the tolerance band must not flip decisions
    # across repeated evaluations.
    decisions_strict = [
        model_strict.check_convergence(1.0, 1.0 - 1.00000005e-7)[0]
        for _ in range(5)]
    decisions_banded = [
        model_banded.check_convergence(1.0, 1.0 - 1.00000005e-7)[0]
        for _ in range(5)]
    assert len(set(decisions_strict)) == 1
    assert all(decisions_banded)


def test_subnormal_protection_zeroes_tiny_improvement():
    config = EngineConfig(subnormal_protection=True)
    model = LinearRegressor(2, config)
    converged, improvement, _ = model.check_convergence(
        1.0, 1.0 - np.finfo(np.float64).tiny)
    assert improvement == 0.0
    assert converged


def test_flush_subnormals_helper():
    values = np.array([np.finfo(np.float64).tiny / 2.0, 1.0,
                       -(np.finfo(np.float64).tiny / 4.0)])
    flushed = flush_subnormals(values.astype(np.float64))
    assert flushed[0] == 0.0
    assert flushed[1] == 1.0
    assert flushed[2] == 0.0

    f32_values = np.array([np.finfo(np.float32).tiny / 2.0],
                          dtype=np.float32)
    assert flush_subnormals(f32_values)[0] == np.float32(0.0)


def test_convergence_decision_is_deterministic():
    config = EngineConfig(convergence_strategy="tolerance_band")
    model = LinearRegressor(2, config)
    first = [model.check_convergence(0.5, 0.5 - 1e-8) for _ in range(3)]
    second = [model.check_convergence(0.5, 0.5 - 1e-8) for _ in range(3)]
    assert first == second
