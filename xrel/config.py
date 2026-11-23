"""Typed pipeline configuration with explicit validation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Optional

from .errors import (
    ConfigInvalid,
    E_THRESHOLD_STRATEGY_INVALID,
    E_THRESHOLD_TOLERANCE_INVALID,
    XrelError,
)
from .numerics import FixedConfig


STRICT = "strict"
TOLERANCE_BAND = "tolerance_band"
THRESHOLD_STRATEGIES = frozenset((STRICT, TOLERANCE_BAND))

# Explicit merge rules used when one document is hit by several candidates.
MERGE_SUM = "sum"
MERGE_MAX = "max"
MERGE_RULES = frozenset((MERGE_SUM, MERGE_MAX))


@dataclass(frozen=True)
class ExpandConfig:
    max_candidates: int = 8
    min_confidence: float = 0.4

    def validate(self) -> None:
        if not isinstance(self.max_candidates, int) or isinstance(self.max_candidates, bool):
            raise ConfigInvalid("max_candidates must be an integer", "expand.max_candidates")
        if self.max_candidates <= 0:
            raise ConfigInvalid("max_candidates must be positive", "expand.max_candidates")
        if not 0.0 <= float(self.min_confidence) <= 1.0:
            raise ConfigInvalid(
                "min_confidence must lie within [0.0, 1.0]",
                "expand.min_confidence",
            )


@dataclass(frozen=True)
class Bm25Config:
    k1: float = 1.5
    b: float = 0.75
    merge_rule: str = MERGE_SUM

    def validate(self) -> None:
        if self.k1 < 0:
            raise ConfigInvalid("bm25.k1 must be non-negative", "bm25.k1")
        if not 0.0 <= self.b <= 1.0:
            raise ConfigInvalid("bm25.b must lie within [0.0, 1.0]", "bm25.b")
        if self.merge_rule not in MERGE_RULES:
            raise ConfigInvalid("unknown document merge rule", "bm25.merge_rule")


@dataclass(frozen=True)
class ThresholdConfig:
    """Relevance grade boundaries plus the comparison strategy."""

    strategy: str = TOLERANCE_BAND
    tolerance: float = 1e-6
    grades: tuple = (("relevant", 6.0), ("weak", 3.0), ("irrelevant", float("-inf")))

    def validate(self) -> None:
        if self.strategy not in THRESHOLD_STRATEGIES:
            raise XrelError(
                E_THRESHOLD_STRATEGY_INVALID,
                "threshold comparison strategy is unknown",
                {"field": "threshold.strategy", "value": self.strategy},
            )
        tolerance = float(self.tolerance)
        if tolerance < 0.0 or math_isnan(tolerance) or math_isinf(tolerance):
            raise XrelError(
                E_THRESHOLD_TOLERANCE_INVALID,
                "threshold tolerance must be a finite non-negative number",
                {"field": "threshold.tolerance", "value": self.tolerance},
            )
        boundaries = [boundary for _, boundary in self.grades if boundary != float("-inf")]
        if boundaries != sorted(boundaries, reverse=True):
            raise ConfigInvalid("grade boundaries must be strictly descending", "threshold.grades")


def math_isnan(value: float) -> bool:
    return value != value


def math_isinf(value: float) -> bool:
    return value in (float("inf"), float("-inf"))


@dataclass(frozen=True)
class ResourceLimits:
    max_posting_reads: int = 200_000
    max_candidate_comparisons: int = 500_000
    max_resident_detail_bytes: int = 2_000_000
    allow_streaming_degrade: bool = True

    def validate(self) -> None:
        for field_name in ("max_posting_reads", "max_candidate_comparisons", "max_resident_detail_bytes"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise ConfigInvalid(f"{field_name} must be a positive integer", f"limits.{field_name}")


@dataclass(frozen=True)
class AppConfig:
    precision: str = "float64"
    fixed: Optional[FixedConfig] = None
    expand: ExpandConfig = field(default_factory=ExpandConfig)
    bm25: Bm25Config = field(default_factory=Bm25Config)
    threshold: ThresholdConfig = field(default_factory=ThresholdConfig)
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def validate(self) -> None:
        if self.precision not in ("float64", "float32", "fixed"):
            raise ConfigInvalid("unknown precision tier", "precision")
        if self.precision == "fixed" and self.fixed is None:
            raise ConfigInvalid("fixed precision requires fixed config", "fixed")
        if self.fixed is not None:
            self.fixed.validate()
        self.expand.validate()
        self.bm25.validate()
        self.threshold.validate()
        self.limits.validate()

    def with_precision(self, precision: str, fixed: Optional[FixedConfig] = None) -> "AppConfig":
        return replace(self, precision=precision, fixed=fixed)


def default_config(precision: str = "float64", fixed_scale: int = 1_000_000) -> AppConfig:
    from .numerics import RoundingMode

    fixed = FixedConfig(scale=fixed_scale, rounding=RoundingMode.HALF_EVEN)
    config = AppConfig(precision=precision, fixed=fixed)
    config.validate()
    return config
