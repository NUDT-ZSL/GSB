"""Convergence decisions with strict and tolerance-band strategies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import TrainingConfig


def _flush_subnormal(value: float, floor: float) -> tuple[float, bool]:
    if not math.isfinite(value):
        return value, False
    if abs(value) < floor:
        return 0.0, True
    return value, False


@dataclass(frozen=True)
class ConvergenceDecision:
    round_number: int
    previous_loss: float
    current_loss: float
    improvement: float
    relative_improvement: float
    loss_scale: float
    effective_threshold: float
    minimum_improvement: float
    relative_tolerance: float
    strict_guard_band: float
    tolerance_band: float
    subnormal_floor: float
    subnormal_flushed: bool
    strict_converged: bool
    band_converged: bool
    selected_strategy: str
    selected_converged: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "previous_loss": self.previous_loss,
            "current_loss": self.current_loss,
            "improvement": self.improvement,
            "relative_improvement": self.relative_improvement,
            "loss_scale": self.loss_scale,
            "effective_threshold": self.effective_threshold,
            "minimum_improvement": self.minimum_improvement,
            "relative_tolerance": self.relative_tolerance,
            "strict_guard_band": self.strict_guard_band,
            "tolerance_band": self.tolerance_band,
            "subnormal_floor": self.subnormal_floor,
            "subnormal_flushed": self.subnormal_flushed,
            "strict_converged": self.strict_converged,
            "band_converged": self.band_converged,
            "selected_strategy": self.selected_strategy,
            "selected_converged": self.selected_converged,
        }


def decide_convergence(
    round_number: int,
    previous_loss: float,
    current_loss: float,
    config: TrainingConfig,
    *,
    dtype_epsilon: float = 2.220446049250313e-16,
) -> ConvergenceDecision:
    """Evaluate both policies on identical evidence.

    ``strict`` compares directly against the effective improvement threshold.
    ``tolerance_band`` treats the band around that threshold as a stable
    no-decision boundary and classifies the whole band as converged. That
    prevents a sub-ulp precision change from flipping the stopping decision.
    """

    floor = max(
        float(config.subnormal_floor),
        float(dtype_epsilon) * 16.0,
    )
    previous, flushed_previous = _flush_subnormal(float(previous_loss), floor)
    current, flushed_current = _flush_subnormal(float(current_loss), floor)
    raw_improvement = previous - current
    improvement, flushed_improvement = _flush_subnormal(raw_improvement, floor)
    loss_scale = max(1.0, abs(previous), abs(current))
    relative_tolerance = float(config.convergence_tolerance) * loss_scale
    effective_threshold = max(float(config.min_improvement), relative_tolerance)
    # Use the widest supported float epsilon for the tie guard so that the
    # strict/band classification is common to float64, float32 and fixed point.
    strict_guard = max(
        64.0 * float(np.finfo(np.float32).eps) * loss_scale,
        floor,
    )
    band = max(
        float(config.tolerance_band_factor) * relative_tolerance,
        strict_guard,
        floor,
    )
    relative_improvement = improvement / loss_scale
    strict = improvement <= effective_threshold + strict_guard
    # The protected strategy deliberately gives the entire threshold band a
    # single converged interpretation.
    band_converged = improvement <= effective_threshold + band
    selected = strict if config.convergence_strategy == "strict" else band_converged
    return ConvergenceDecision(
        round_number=int(round_number),
        previous_loss=previous,
        current_loss=current,
        improvement=improvement,
        relative_improvement=relative_improvement,
        loss_scale=loss_scale,
        effective_threshold=effective_threshold,
        minimum_improvement=float(config.min_improvement),
        relative_tolerance=relative_tolerance,
        strict_guard_band=strict_guard,
        tolerance_band=band,
        subnormal_floor=floor,
        subnormal_flushed=flushed_previous or flushed_current or flushed_improvement,
        strict_converged=bool(strict),
        band_converged=bool(band_converged),
        selected_strategy=config.convergence_strategy,
        selected_converged=bool(selected),
    )


def convergence_summary(
    decisions: list[dict[str, Any]],
    *,
    selected_strategy: str,
    max_iterations: int,
    stop_reason: str,
    final_round: int,
) -> dict[str, Any]:
    strict_round = next(
        (item["round"] for item in decisions if item["strict_converged"]), None
    )
    band_round = next(
        (item["round"] for item in decisions if item["band_converged"]), None
    )
    return {
        "selected_strategy": selected_strategy,
        "max_iterations": max_iterations,
        "final_round": final_round,
        "stop_reason": stop_reason,
        "first_strict_convergence_round": strict_round,
        "first_band_convergence_round": band_round,
        "selected_convergence_round": (
            strict_round if selected_strategy == "strict" else band_round
        ),
        "both_policy_conclusions": decisions,
    }
