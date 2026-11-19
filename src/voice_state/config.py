"""Analysis configuration and fixed resource limits."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from .canonical import checksum_value, sanitize_json
from .errors import BusinessError, E_CONFIG_INVALID


@dataclass(frozen=True, slots=True)
class AnalysisConfig:
    sample_rate: int = 16000
    frame_ms: float = 20.0
    hop_ms: float = 20.0
    feature_places: int = 10
    score_places: int = 12
    confidence_z: float = 1.96
    model_sigma: float = 0.035
    max_dialogs_per_stage: int = 1000
    max_resident_feature_bytes: int = 64 * 1024 * 1024
    max_segment_memory_bytes: int = 32 * 1024 * 1024
    batch_size: int = 0

    def validate(self) -> None:
        if self.sample_rate != 16000:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Only 16 kHz PCM is supported",
                {"field": "sample_rate", "expected": 16000, "actual": self.sample_rate},
            )
        if self.frame_ms <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "frame_ms must be positive",
                {"field": "frame_ms", "actual": self.frame_ms},
            )
        if self.hop_ms <= 0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "hop_ms must be positive",
                {"field": "hop_ms", "actual": self.hop_ms},
            )
        if self.hop_ms > self.frame_ms:
            raise BusinessError(
                E_CONFIG_INVALID,
                "hop_ms must not exceed frame_ms",
                {"field": "hop_ms", "actual": self.hop_ms, "frame_ms": self.frame_ms},
            )
        if not 2 <= self.feature_places <= 15:
            raise BusinessError(
                E_CONFIG_INVALID,
                "feature_places is outside the supported range",
                {"field": "feature_places", "actual": self.feature_places},
            )
        if not 2 <= self.score_places <= 15:
            raise BusinessError(
                E_CONFIG_INVALID,
                "score_places is outside the supported range",
                {"field": "score_places", "actual": self.score_places},
            )
        for name in (
            "max_dialogs_per_stage",
            "max_resident_feature_bytes",
            "max_segment_memory_bytes",
            "batch_size",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value < 0:
                raise BusinessError(
                    E_CONFIG_INVALID,
                    f"{name} must be a non-negative integer",
                    {"field": name, "actual": value},
                )
        if not 0.0 < self.confidence_z <= 5.0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "confidence_z is outside the supported range",
                {"field": "confidence_z", "actual": self.confidence_z},
            )
        if not 0.0 <= self.model_sigma <= 1.0:
            raise BusinessError(
                E_CONFIG_INVALID,
                "model_sigma is outside the supported range",
                {"field": "model_sigma", "actual": self.model_sigma},
            )

    def frame_samples(self) -> int:
        return max(1, int(round(self.frame_ms * self.sample_rate / 1000.0)))

    def hop_samples(self) -> int:
        return max(1, int(round(self.hop_ms * self.sample_rate / 1000.0)))

    def as_dict(self) -> dict[str, Any]:
        return sanitize_json(asdict(self))

    def checksum(self) -> str:
        return checksum_value(self.as_dict())

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AnalysisConfig":
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(values) - allowed)
        if unknown:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Unknown configuration field",
                {"field": unknown[0]},
            )
        try:
            config = cls(**values)
        except TypeError as exc:
            raise BusinessError(
                E_CONFIG_INVALID,
                "Invalid configuration value",
                {"field": "config"},
            ) from exc
        config.validate()
        return config

