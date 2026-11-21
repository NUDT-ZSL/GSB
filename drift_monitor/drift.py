"""Per-feature drift tests and severity classification."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .config_model import (
    CATEGORICAL,
    MEAN_SHIFT,
    NUMERIC,
    PROPORTION_DIST,
    QUANTILE_DIST,
    FeatureSpec,
)
from .errors import DriftError, ErrorCode
from .statistics import (
    BatchStat,
    CategoricalStat,
    NumericStat,
    describe_categorical,
    describe_numeric,
)

SEVERITY_NONE = "none"
SEVERITY_LOW = "low"
SEVERITY_MEDIUM = "medium"
SEVERITY_HIGH = "high"


@dataclass
class FeatureResult:
    feature: str
    column_type: str
    method: str
    drift_amount: float
    threshold: float
    triggered: bool
    severity: str
    window_start: int
    window_end: int
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature": self.feature,
            "column_type": self.column_type,
            "method": self.method,
            "drift_amount": self.drift_amount,
            "threshold": self.threshold,
            "triggered": self.triggered,
            "severity": self.severity,
            "window_start": self.window_start,
            "window_end": self.window_end,
            "detail": self.detail,
        }


def classify_severity(ratio: float) -> str:
    if ratio < 1.0:
        return SEVERITY_NONE
    if ratio < 1.5:
        return SEVERITY_LOW
    if ratio < 2.5:
        return SEVERITY_MEDIUM
    return SEVERITY_HIGH


def evaluate_feature(
    spec: FeatureSpec,
    window_numeric: Optional[NumericStat],
    window_categorical: Optional[CategoricalStat],
    current: BatchStat,
    window_start: int,
    window_end: int,
) -> FeatureResult:
    method = spec.methods[0]
    if spec.column_type == NUMERIC:
        return _evaluate_numeric(
            spec, method, window_numeric, current, window_start, window_end
        )
    return _evaluate_categorical(
        spec, method, window_categorical, current, window_start, window_end
    )


def _evaluate_numeric(
    spec: FeatureSpec,
    method: str,
    window_stat: Optional[NumericStat],
    current: BatchStat,
    window_start: int,
    window_end: int,
) -> FeatureResult:
    current_stat = current.numeric[spec.name]
    if method not in (MEAN_SHIFT, QUANTILE_DIST):
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            f"unknown numeric test method {method!r}",
            {"field": "methods", "feature": spec.name},
        )
    if spec.threshold <= 0.0:
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            "threshold must be positive",
            {"field": "threshold", "feature": spec.name},
        )
    if current_stat.count < spec.min_samples:
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            "current batch has fewer observed values than min_samples",
            {
                "field": "min_samples",
                "feature": spec.name,
                "batch_id": current.batch_id,
                "observed": current_stat.count,
                "min_samples": spec.min_samples,
            },
        )

    detail: Dict[str, Any] = {
        "reference": describe_numeric(window_stat, spec.quantiles),
        "current": describe_numeric(current_stat, spec.quantiles),
    }
    if method == MEAN_SHIFT:
        scale = max(window_stat.variance ** 0.5, 1e-12)
        drift_amount = abs(current_stat.mean - window_stat.mean) / scale
        detail["reference_mean"] = window_stat.mean
        detail["current_mean"] = current_stat.mean
        detail["reference_std"] = scale
    else:
        ref_q = window_stat.quantiles(spec.quantiles)
        cur_q = current_stat.quantiles(spec.quantiles)
        scale = max(window_stat.variance ** 0.5, 1e-12)
        drift_amount = (
            sum(abs(c - r) for c, r in zip(cur_q, ref_q)) / len(spec.quantiles)
        ) / scale
        detail["reference_quantiles"] = list(ref_q)
        detail["current_quantiles"] = list(cur_q)

    triggered = drift_amount >= spec.threshold
    ratio = drift_amount / spec.threshold
    return FeatureResult(
        feature=spec.name,
        column_type=NUMERIC,
        method=method,
        drift_amount=float(drift_amount),
        threshold=float(spec.threshold),
        triggered=triggered,
        severity=classify_severity(ratio),
        window_start=window_start,
        window_end=window_end,
        detail=detail,
    )


def _evaluate_categorical(
    spec: FeatureSpec,
    method: str,
    window_stat: Optional[CategoricalStat],
    current: BatchStat,
    window_start: int,
    window_end: int,
) -> FeatureResult:
    if method != PROPORTION_DIST:
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            f"categorical column cannot use method {method!r}",
            {"field": "methods", "feature": spec.name},
        )
    if not (0.0 < spec.threshold <= 1.0):
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            "categorical proportion threshold must lie inside (0, 1]",
            {"field": "threshold", "feature": spec.name},
        )
    current_stat = current.categorical[spec.name]
    if current_stat.observed < spec.min_samples:
        raise DriftError(
            ErrorCode.CONFIG_INVALID,
            "current batch has fewer observed values than min_samples",
            {
                "field": "min_samples",
                "feature": spec.name,
                "batch_id": current.batch_id,
                "observed": current_stat.observed,
                "min_samples": spec.min_samples,
            },
        )

    levels = sorted(set(window_stat.frequencies) | set(current_stat.frequencies))
    ref_props = window_stat.proportions(levels)
    cur_props = current_stat.proportions(levels)
    # Total variation distance: half of the L1 distance, range [0, 1].
    drift_amount = 0.5 * sum(abs(cur_props[level] - ref_props[level]) for level in levels)
    triggered = drift_amount >= spec.threshold
    ratio = drift_amount / spec.threshold
    detail = {
        "levels": levels,
        "reference_proportions": ref_props,
        "current_proportions": cur_props,
        "reference": describe_categorical(window_stat),
        "current": describe_categorical(current_stat),
    }
    return FeatureResult(
        feature=spec.name,
        column_type=CATEGORICAL,
        method=method,
        drift_amount=float(drift_amount),
        threshold=float(spec.threshold),
        triggered=triggered,
        severity=classify_severity(ratio),
        window_start=window_start,
        window_end=window_end,
        detail=detail,
    )


def sort_results(results: Sequence[FeatureResult]) -> List[FeatureResult]:
    """Deterministic ordering: triggered first, then amount desc, then name."""
    return sorted(
        results,
        key=lambda item: (
            not item.triggered,
            -round(item.drift_amount, 12),
            item.feature,
        ),
    )
