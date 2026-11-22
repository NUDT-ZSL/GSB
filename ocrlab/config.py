"""Versioned recognition configuration."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any

from .errors import BusinessError, E_INVALID_CONFIG
from .serialization import checksum


@dataclass(frozen=True)
class PipelineConfig:
    version: str = "rec-v1"
    binarize_threshold: int = 128
    line_min_ink: int = 3
    line_padding: int = 1
    max_lines_per_page: int = 40
    max_candidate_comparisons: int = 250_000
    max_feature_memory_bytes: int = 64 * 1024 * 1024
    resource_policy: str = "stream"
    recognition_chunk_lines: int = 3
    aggregate_confidence_threshold: float = 0.50
    fallback_confidence_threshold: float = 0.55
    comparison_score_tolerance: float = 1e-4
    comparison_coordinate_tolerance: float = 0.5
    normalization_epsilon: float = 1e-6
    candidates_per_char: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def config_checksum(self) -> str:
        return checksum(self.to_dict())

    def validate(self) -> None:
        if not isinstance(self.version, str) or not self.version:
            raise BusinessError(
                E_INVALID_CONFIG,
                "config version must be a non-empty string",
                stage="config",
                field="version",
            )
        if not 0 < int(self.binarize_threshold) <= 255:
            raise BusinessError(
                E_INVALID_CONFIG,
                "binarize_threshold must be in [1, 255]",
                stage="config",
                field="binarize_threshold",
            )
        if int(self.line_min_ink) <= 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "line_min_ink must be positive",
                stage="config",
                field="line_min_ink",
            )
        if int(self.line_padding) < 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "line_padding must be non-negative",
                stage="config",
                field="line_padding",
            )
        if int(self.max_lines_per_page) <= 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "max_lines_per_page must be positive",
                stage="config",
                field="max_lines_per_page",
            )
        if int(self.max_candidate_comparisons) <= 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "max_candidate_comparisons must be positive",
                stage="config",
                field="max_candidate_comparisons",
            )
        if int(self.max_feature_memory_bytes) <= 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "max_feature_memory_bytes must be positive",
                stage="config",
                field="max_feature_memory_bytes",
            )
        if self.resource_policy not in {"error", "stream"}:
            raise BusinessError(
                E_INVALID_CONFIG,
                "resource_policy must be 'error' or 'stream'",
                stage="config",
                field="resource_policy",
            )
        if int(self.recognition_chunk_lines) <= 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "recognition_chunk_lines must be positive",
                stage="config",
                field="recognition_chunk_lines",
            )
        if not 0.0 <= float(self.aggregate_confidence_threshold) <= 1.0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "aggregate_confidence_threshold must be in [0, 1]",
                stage="config",
                field="aggregate_confidence_threshold",
            )
        if not 0.0 <= float(self.fallback_confidence_threshold) <= 1.0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "fallback_confidence_threshold must be in [0, 1]",
                stage="config",
                field="fallback_confidence_threshold",
            )
        if float(self.comparison_score_tolerance) < 0.0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "comparison_score_tolerance must be non-negative",
                stage="config",
                field="comparison_score_tolerance",
            )
        if int(self.candidates_per_char) <= 0:
            raise BusinessError(
                E_INVALID_CONFIG,
                "candidates_per_char must be positive",
                stage="config",
                field="candidates_per_char",
            )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineConfig":
        allowed = {item.name for item in fields(cls)}
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise BusinessError(
                E_INVALID_CONFIG,
                f"unknown config fields: {', '.join(unknown)}",
                stage="config",
                field=unknown[0],
            )
        config = cls(**value)
        config.validate()
        return config

