"""Validated configuration objects shared by the deterministic pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from .errors import DomainError, E_CONFIG_INVALID


VALID_TIERS = ("float64", "float32", "fixed")
VALID_ROUNDING_MODES = ("half_even", "half_away", "toward_zero", "floor", "ceil")
VALID_COMPARE_STRATEGIES = ("strict", "tolerance")


def _samples_for_ms(value: float, sample_rate: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DomainError(
            E_CONFIG_INVALID,
            f"{field_name} must be numeric",
            field_name=field_name,
        )
    fraction = Fraction(str(value)) * sample_rate / 1000
    if fraction.denominator != 1 or fraction <= 0:
        raise DomainError(
            E_CONFIG_INVALID,
            f"{field_name}={value!r} does not map to a positive whole sample count",
            field_name=field_name,
        )
    return int(fraction)


@dataclass(frozen=True)
class FrameConfig:
    sample_rate: int = 16000
    frame_ms: float = 20.0
    hop_ms: float = 20.0
    num_phonemes: int = 8

    def validate(self) -> None:
        if self.sample_rate != 16000:
            raise DomainError(
                E_CONFIG_INVALID,
                "the executable pipeline requires a 16000 Hz sample rate",
                field_name="sample_rate",
            )
        if isinstance(self.num_phonemes, bool) or not isinstance(self.num_phonemes, int) or self.num_phonemes <= 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "num_phonemes must be a positive integer",
                field_name="num_phonemes",
            )
        frame_length = _samples_for_ms(self.frame_ms, self.sample_rate, "frame_ms")
        hop_length = _samples_for_ms(self.hop_ms, self.sample_rate, "hop_ms")
        if hop_length > frame_length:
            raise DomainError(
                E_CONFIG_INVALID,
                "hop_ms must not exceed frame_ms",
                field_name="hop_ms",
            )

    @property
    def frame_samples(self) -> int:
        self.validate()
        return _samples_for_ms(self.frame_ms, self.sample_rate, "frame_ms")

    @property
    def hop_samples(self) -> int:
        self.validate()
        return _samples_for_ms(self.hop_ms, self.sample_rate, "hop_ms")

    def sample_count_for_frames(self, frame_count: int) -> int:
        if frame_count <= 0:
            raise ValueError("frame_count must be positive")
        return (frame_count - 1) * self.hop_samples + self.frame_samples

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "hop_ms": self.hop_ms,
            "num_phonemes": self.num_phonemes,
        }


@dataclass(frozen=True)
class PrecisionConfig:
    tier: str = "float64"
    fixed_scale: int = 1 << 20
    rounding_mode: str = "half_even"
    feature_scale: float = 0.75
    epsilon: float = 1e-12

    def validate(self) -> None:
        if self.tier not in VALID_TIERS:
            raise DomainError(
                E_CONFIG_INVALID,
                f"unknown precision tier {self.tier!r}; expected one of {VALID_TIERS}",
                field_name="precision_tier",
            )
        if isinstance(self.fixed_scale, bool) or not isinstance(self.fixed_scale, int):
            raise DomainError(
                E_CONFIG_INVALID,
                "fixed_scale must be an integer",
                field_name="fixed_scale",
            )
        if self.fixed_scale <= 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "fixed_scale must be strictly positive",
                field_name="fixed_scale",
            )
        if self.rounding_mode not in VALID_ROUNDING_MODES:
            raise DomainError(
                E_CONFIG_INVALID,
                f"unknown rounding mode {self.rounding_mode!r}",
                field_name="rounding_mode",
            )
        if not (self.feature_scale > 0 and self.feature_scale < 1e6):
            raise DomainError(
                E_CONFIG_INVALID,
                "feature_scale must be finite, positive and reasonably bounded",
                field_name="feature_scale",
            )
        if not (self.epsilon > 0):
            raise DomainError(
                E_CONFIG_INVALID,
                "epsilon must be strictly positive",
                field_name="epsilon",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier": self.tier,
            "fixed_scale": self.fixed_scale,
            "rounding_mode": self.rounding_mode,
            "feature_scale": self.feature_scale,
            "epsilon": self.epsilon,
        }


@dataclass(frozen=True)
class SimilarityConfig:
    threshold: float = 0.70
    tolerance: float = 1e-6
    top_k: int = 3
    compare_strategy: str = "tolerance"
    subnormal_floor: float = 2.2250738585072014e-308

    def validate(self) -> None:
        if not isinstance(self.threshold, (int, float)) or isinstance(self.threshold, bool):
            raise DomainError(
                E_CONFIG_INVALID,
                "threshold must be numeric",
                field_name="threshold",
            )
        if not (-1.0 <= float(self.threshold) <= 1.0):
            raise DomainError(
                E_CONFIG_INVALID,
                "threshold must lie in [-1, 1]",
                field_name="threshold",
            )
        if not (self.tolerance > 0 and self.tolerance < 1.0):
            raise DomainError(
                E_CONFIG_INVALID,
                "tolerance must be strictly positive and below 1",
                field_name="tolerance",
            )
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int) or self.top_k <= 0:
            raise DomainError(
                E_CONFIG_INVALID,
                "top_k must be a positive integer",
                field_name="top_k",
            )
        if self.compare_strategy not in VALID_COMPARE_STRATEGIES:
            raise DomainError(
                E_CONFIG_INVALID,
                f"compare_strategy must be one of {VALID_COMPARE_STRATEGIES}",
                field_name="compare_strategy",
            )
        if not (self.subnormal_floor > 0):
            raise DomainError(
                E_CONFIG_INVALID,
                "subnormal_floor must be strictly positive",
                field_name="subnormal_floor",
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "threshold": self.threshold,
            "tolerance": self.tolerance,
            "top_k": self.top_k,
            "compare_strategy": self.compare_strategy,
            "subnormal_floor": self.subnormal_floor,
        }


@dataclass(frozen=True)
class PipelineConfig:
    frame: FrameConfig = FrameConfig()
    tiers: tuple[str, ...] = ("float64", "float32", "fixed")
    fixed_scale: int = 1 << 20
    rounding_mode: str = "half_even"
    feature_scale: float = 0.75
    epsilon: float = 1e-12
    similarity: SimilarityConfig = SimilarityConfig()

    def validate(self) -> None:
        self.frame.validate()
        if not self.tiers:
            raise DomainError(E_CONFIG_INVALID, "tiers cannot be empty", field_name="tiers")
        if len(set(self.tiers)) != len(self.tiers):
            raise DomainError(E_CONFIG_INVALID, "tiers must be unique", field_name="tiers")
        if "float64" not in self.tiers:
            raise DomainError(
                E_CONFIG_INVALID,
                "the float64 tier is required as the snapshot reference truth",
                field_name="tiers",
            )
        for tier in self.tiers:
            PrecisionConfig(
                tier=tier,
                fixed_scale=self.fixed_scale,
                rounding_mode=self.rounding_mode,
                feature_scale=self.feature_scale,
                epsilon=self.epsilon,
            ).validate()
        self.similarity.validate()

    def precision(self, tier: str) -> PrecisionConfig:
        config = PrecisionConfig(
            tier=tier,
            fixed_scale=self.fixed_scale,
            rounding_mode=self.rounding_mode,
            feature_scale=self.feature_scale,
            epsilon=self.epsilon,
        )
        config.validate()
        return config

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame": self.frame.to_dict(),
            "tiers": list(self.tiers),
            "fixed_scale": self.fixed_scale,
            "rounding_mode": self.rounding_mode,
            "feature_scale": self.feature_scale,
            "epsilon": self.epsilon,
            "similarity": self.similarity.to_dict(),
        }
