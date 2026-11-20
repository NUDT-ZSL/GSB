"""Immutable configuration objects and validation."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .canonical import hash_value
from .errors import E_FRAME_CONFIG, E_INPUT_INVALID, E_RESOURCE_LIMIT, raise_error


SAMPLE_RATE = 16_000


def _require_finite_positive(name: str, value: float) -> None:
    if not isinstance(value, (int, float)) or not float(value) > 0.0:
        raise_error(E_FRAME_CONFIG, f"{name} must be a positive finite number", field=name)
    if float(value) != float(value) or float(value) in (float("inf"), float("-inf")):
        raise_error(E_FRAME_CONFIG, f"{name} must be finite", field=name)


@dataclass(frozen=True)
class FeatureConfig:
    """Replaceable acoustic feature extraction parameters.

    ``version`` is an explicit domain version.  ``params_hash`` also covers every
    numeric parameter, so two differently configured extractors cannot collide
    merely because they reuse the same label.
    """

    version: str = "acoustic-v1"
    frame_ms: float = 20.0
    frame_shift_ms: float = 20.0
    fft_size: int = 512
    rolloff_percent: float = 0.85
    window: str = "hann-periodic"
    dtype: str = "float64"

    def validate(self, sample_rate: int = SAMPLE_RATE) -> None:
        if sample_rate != SAMPLE_RATE:
            from .errors import E_SAMPLE_RATE

            raise_error(
                E_SAMPLE_RATE,
                "only 16 kHz mono audio is supported",
                actual=sample_rate,
                expected=SAMPLE_RATE,
            )
        _require_finite_positive("frame_ms", self.frame_ms)
        _require_finite_positive("frame_shift_ms", self.frame_shift_ms)
        if self.frame_shift_ms > self.frame_ms:
            raise_error(
                E_FRAME_CONFIG,
                "frame_shift_ms must not exceed frame_ms",
                frame_ms=self.frame_ms,
                frame_shift_ms=self.frame_shift_ms,
            )
        if int(self.fft_size) <= 0:
            raise_error(E_FRAME_CONFIG, "fft_size must be positive", fft_size=self.fft_size)
        if not 0.0 < float(self.rolloff_percent) <= 1.0:
            raise_error(
                E_FRAME_CONFIG,
                "rolloff_percent must be in (0, 1]",
                rolloff_percent=self.rolloff_percent,
            )
        if self.window not in {"hann-periodic", "rectangular"}:
            raise_error(E_FRAME_CONFIG, "unsupported window", window=self.window)
        if self.dtype not in {"float64", "float32"}:
            raise_error(E_FRAME_CONFIG, "unsupported feature dtype", dtype=self.dtype)
        if self.frame_samples(sample_rate) <= 0 or self.hop_samples(sample_rate) <= 0:
            raise_error(
                E_FRAME_CONFIG,
                "frame and hop must each contain at least one sample",
                frame_ms=self.frame_ms,
                frame_shift_ms=self.frame_shift_ms,
            )

    def frame_samples(self, sample_rate: int = SAMPLE_RATE) -> int:
        return int(round(float(self.frame_ms) * sample_rate / 1000.0))

    def hop_samples(self, sample_rate: int = SAMPLE_RATE) -> int:
        return int(round(float(self.frame_shift_ms) * sample_rate / 1000.0))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        return hash_value({"kind": "feature-params", **self.to_dict()})

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FeatureConfig":
        return cls(**value)


@dataclass(frozen=True)
class SpeakerConfig:
    """Speaker scoring and segmentation configuration.

    ``segment_threshold`` is the minimum continuous segment length in frames.
    Runs shorter than this are deterministically merged into a neighbour.
    """

    version: str = "speaker-v1"
    segment_threshold: int = 1
    similarity: str = "diagonal_mahalanobis"
    tie_break: str = "speaker_id_asc"

    def validate(self) -> None:
        if not isinstance(self.segment_threshold, int) or self.segment_threshold < 1:
            raise_error(
                E_FRAME_CONFIG,
                "segment_threshold must be a positive integer",
                segment_threshold=self.segment_threshold,
            )
        if self.similarity not in {"diagonal_mahalanobis", "cosine"}:
            raise_error(E_INPUT_INVALID, "unsupported similarity", similarity=self.similarity)
        if self.tie_break != "speaker_id_asc":
            raise_error(E_INPUT_INVALID, "unsupported tie break", tie_break=self.tie_break)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        return hash_value({"kind": "speaker-params", **self.to_dict()})


@dataclass(frozen=True)
class RegressionConfig:
    version: str = "regression-v2"
    ridge_lambda: float = 1e-3
    confidence_z: float = 1.96
    prediction_scheme: str = "leave_one_out"

    def validate(self) -> None:
        if float(self.ridge_lambda) < 0.0:
            raise_error(
                E_INPUT_INVALID,
                "ridge_lambda must be non-negative",
                ridge_lambda=self.ridge_lambda,
            )
        if float(self.confidence_z) <= 0.0:
            raise_error(
                E_INPUT_INVALID,
                "confidence_z must be positive",
                confidence_z=self.confidence_z,
            )
        if self.prediction_scheme not in {"leave_one_out", "in_sample"}:
            raise_error(
                E_INPUT_INVALID,
                "unsupported regression prediction scheme",
                prediction_scheme=self.prediction_scheme,
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        return hash_value({"kind": "regression-params", **self.to_dict()})


@dataclass(frozen=True)
class AggregationConfig:
    version: str = "aggregation-v1"
    weight_basis: str = "segment_duration_ms"
    confidence_z: float = 1.96

    def validate(self) -> None:
        if self.weight_basis not in {"segment_duration_ms", "segment_frame_count"}:
            raise_error(
                E_INPUT_INVALID,
                "unsupported weight basis",
                weight_basis=self.weight_basis,
            )
        if float(self.confidence_z) <= 0.0:
            raise_error(
                E_INPUT_INVALID,
                "confidence_z must be positive",
                confidence_z=self.confidence_z,
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        return hash_value({"kind": "aggregation-params", **self.to_dict()})


@dataclass(frozen=True)
class ResourceLimits:
    max_frames_per_analysis: int = 200_000
    max_cache_entries: int = 100_000
    max_feature_bytes: int = 256 * 1024 * 1024
    on_limit: str = "error"

    def validate(self) -> None:
        for name in (
            "max_frames_per_analysis",
            "max_cache_entries",
            "max_feature_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 1:
                raise_error(
                    E_INPUT_INVALID,
                    f"{name} must be a positive integer",
                    field=name,
                    value=value,
                )
        if self.on_limit not in {"error", "degrade"}:
            raise_error(
                E_INPUT_INVALID,
                "on_limit must be 'error' or 'degrade'",
                on_limit=self.on_limit,
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def params_hash(self) -> str:
        return hash_value({"kind": "resource-limits", **self.to_dict()})


@dataclass(frozen=True)
class PipelineConfig:
    feature: FeatureConfig = FeatureConfig()
    speaker: SpeakerConfig = SpeakerConfig()
    regression: RegressionConfig = RegressionConfig()
    aggregation: AggregationConfig = AggregationConfig()
    resources: ResourceLimits = ResourceLimits()

    def validate(self, sample_rate: int = SAMPLE_RATE) -> None:
        self.feature.validate(sample_rate)
        self.speaker.validate()
        self.regression.validate()
        self.aggregation.validate()
        self.resources.validate()

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature.to_dict(),
            "speaker": self.speaker.to_dict(),
            "regression": self.regression.to_dict(),
            "aggregation": self.aggregation.to_dict(),
            "resources": self.resources.to_dict(),
        }

    def params_hash(self) -> str:
        return hash_value({"kind": "pipeline-config", **self.to_dict()})


def check_feature_memory(
    frame_count: int,
    feature_count: int,
    dtype: str,
    max_bytes: int,
) -> int:
    item_size = 8 if dtype == "float64" else 4
    return int(frame_count) * int(feature_count) * item_size


def resource_limit_error(resource: str, **details: Any) -> None:
    raise_error(E_RESOURCE_LIMIT, f"resource limit exceeded: {resource}", resource=resource, **details)
