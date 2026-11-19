"""Typed configuration with strict validation.

Illegal values are never silently replaced by defaults. Constructing a
:class:`ComponentConfig` with an invalid field raises
``E_CONFIG_INVALID`` and names the offending field.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .errors import BusinessError, ErrorCode

SAMPLE_RATE = 16000
SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class ResourceLimits:
    """Hard / soft budgets used by the conflict arbiter."""

    max_resident_frames: int = 200_000
    max_separation_comparisons: int = 50_000_000
    max_regression_terms: int = 5_000_000
    on_exceed: str = "degrade"  # "degrade" or "error"

    def validate(self) -> None:
        for name in (
            "max_resident_frames",
            "max_separation_comparisons",
            "max_regression_terms",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise BusinessError(
                    ErrorCode.E_CONFIG_INVALID,
                    f"{name} must be a positive integer, got {value!r}",
                    field=name,
                )
        if self.on_exceed not in ("degrade", "error"):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "on_exceed must be 'degrade' or 'error'",
                field="on_exceed",
            )


@dataclass(frozen=True)
class PathProfile:
    """Thresholds separating the fine and approximate regimes."""

    max_fine_dialogs: int = 4
    max_fine_total_frames: int = 4000
    max_fine_speakers: int = 4
    max_fine_feature_dim: int = 8

    def validate(self) -> None:
        for name in (
            "max_fine_dialogs",
            "max_fine_total_frames",
            "max_fine_speakers",
            "max_fine_feature_dim",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                raise BusinessError(
                    ErrorCode.E_CONFIG_INVALID,
                    f"{name} must be a positive integer, got {value!r}",
                    field=name,
                )


@dataclass(frozen=True)
class ComponentConfig:
    sample_rate: int = SAMPLE_RATE
    frame_ms: float = 20.0
    hop_ms: float = 20.0
    max_speakers: int = 8
    fine_feature_dim: int = 8
    approx_feature_dim: int = 4
    fine_iterations: int = 12
    confidence_z: float = 1.96
    seed: int = 20261120
    snapshot_version: int = SNAPSHOT_VERSION
    resource_limits: ResourceLimits = field(default_factory=ResourceLimits)
    path_profile: PathProfile = field(default_factory=PathProfile)

    def validate(self) -> None:
        if self.sample_rate != SAMPLE_RATE:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"only sample_rate={SAMPLE_RATE} is supported, got {self.sample_rate}",
                field="sample_rate",
            )
        if not isinstance(self.frame_ms, (int, float)) or not (
            5.0 <= float(self.frame_ms) <= 200.0
        ):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"frame_ms out of legal range [5, 200] ms: {self.frame_ms!r}",
                field="frame_ms",
            )
        if not isinstance(self.hop_ms, (int, float)) or float(self.hop_ms) <= 0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"hop_ms must be positive, got {self.hop_ms!r}",
                field="hop_ms",
            )
        if float(self.hop_ms) > float(self.frame_ms):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"hop_ms ({self.hop_ms}) must not exceed frame_ms ({self.frame_ms})",
                field="hop_ms",
            )
        if not isinstance(self.max_speakers, int) or self.max_speakers <= 0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"max_speakers must be a positive integer, got {self.max_speakers!r}",
                field="max_speakers",
            )
        if self.fine_feature_dim <= 0 or self.approx_feature_dim <= 0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "feature dimensions must be positive",
                field="fine_feature_dim",
            )
        if self.approx_feature_dim > self.fine_feature_dim:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "approx_feature_dim cannot exceed fine_feature_dim",
                field="approx_feature_dim",
            )
        if not isinstance(self.fine_iterations, int) or self.fine_iterations <= 0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "fine_iterations must be a positive integer",
                field="fine_iterations",
            )
        if float(self.confidence_z) <= 0:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "confidence_z must be positive",
                field="confidence_z",
            )
        if not isinstance(self.seed, int):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "seed must be an integer",
                field="seed",
            )
        self.resource_limits.validate()
        self.path_profile.validate()

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "ComponentConfig":
        if not isinstance(data, dict):
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                "configuration root must be an object",
            )
        allowed = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        unknown = set(data) - allowed
        if unknown:
            raise BusinessError(
                ErrorCode.E_CONFIG_INVALID,
                f"unknown configuration fields: {sorted(unknown)}",
                field=sorted(unknown)[0],
            )
        kwargs = dict(data)
        if "resource_limits" in kwargs and kwargs["resource_limits"] is not None:
            kwargs["resource_limits"] = ResourceLimits(**kwargs["resource_limits"])
        if "path_profile" in kwargs and kwargs["path_profile"] is not None:
            kwargs["path_profile"] = PathProfile(**kwargs["path_profile"])
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg
