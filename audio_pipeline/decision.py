"""Dynamic-range budget evaluation and deterministic action suggestions."""

from __future__ import annotations

from .models import Decision, Measurement, UnifiedView
from .snapshots import criterion_data, resolve_numeric
from .util import rounded


def decide(
    measurement: Measurement,
    view: UnifiedView,
    *,
    defaults,
) -> Decision:
    canonical = measurement.canonical_id
    minimum = resolve_numeric(
        view, canonical, "dynamic_range", ("min_db", "dynamic_range_min_db"),
        defaults.dynamic_range_min_db,
    )
    maximum = resolve_numeric(
        view, canonical, "dynamic_range", ("max_db", "dynamic_range_max_db"),
        defaults.dynamic_range_max_db,
    )
    target = resolve_numeric(
        view, canonical, "integrated_loudness", ("target_lufs", "threshold_lufs"),
        resolve_numeric(view, canonical, "loudness", ("target_lufs",),
                        defaults.target_lufs),
    )
    comp_data = criterion_data(view, canonical, "compression") or {}
    comp_enabled = comp_data.get("enabled", True)
    if not isinstance(comp_enabled, bool):
        comp_enabled = True
    comp_threshold = resolve_numeric(
        view, canonical, "compression", ("threshold_lufs", "threshold_db"),
        defaults.compression_threshold_lufs,
    )
    comp_ratio = resolve_numeric(
        view, canonical, "compression", ("ratio",), defaults.compression_ratio
    )

    reasons: list[str] = []
    if measurement.integrated_lufs is None:
        verdict = "unavailable"
        reason = "measurement_unavailable"
        if measurement.silent:
            reason = "silent_input"
        reasons.append(reason)
        return Decision(
            action="none",
            gain_db=0.0,
            limiter=False,
            target_lufs=target,
            dynamic_range_min_db=minimum,
            dynamic_range_max_db=maximum,
            dynamic_range_verdict=verdict,
            compression_threshold_lufs=comp_threshold,
            compression_ratio=comp_ratio,
            reasons=reasons,
            decision_revision=view.decision_revision,
        )

    dr = measurement.dynamic_range_db
    if dr is None:
        verdict = "unavailable"
        reasons.append("dynamic_range_unavailable")
    elif dr < minimum:
        verdict = "below_min"
        reasons.append("dynamic_range_below_min")
    elif dr > maximum:
        verdict = "above_max"
        reasons.append("dynamic_range_above_max")
    else:
        verdict = "within"
        reasons.append("dynamic_range_within_budget")

    gain = round(target - measurement.integrated_lufs, 6)
    if abs(gain) < 0.05:
        gain = 0.0
    limiter = bool(
        comp_enabled
        and (
            measurement.integrated_lufs > comp_threshold
            or verdict == "above_max"
        )
    )
    if limiter:
        reasons.append("compression_threshold_or_budget_exceeded")
    if gain > 0:
        reasons.append("gain_increase_required")
    elif gain < 0:
        reasons.append("gain_reduction_required")
    else:
        reasons.append("target_loudness_met")

    if limiter and gain > 0:
        action = "gain_and_limit"
    elif limiter:
        action = "limit"
    elif gain > 0:
        action = "gain_up"
    elif gain < 0:
        action = "gain_down"
    else:
        action = "none"
    return Decision(
        action=action,
        gain_db=rounded(gain, 6) or 0.0,
        limiter=limiter,
        target_lufs=target,
        dynamic_range_min_db=minimum,
        dynamic_range_max_db=maximum,
        dynamic_range_verdict=verdict,
        compression_threshold_lufs=comp_threshold,
        compression_ratio=comp_ratio,
        reasons=reasons,
        decision_revision=view.decision_revision,
    )
