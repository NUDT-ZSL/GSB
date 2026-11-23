"""Validated, versionable pipeline configuration."""

from __future__ import annotations

import dataclasses
import json
import math
from dataclasses import dataclass, field
from typing import Any

from . import thresholds
from .errors import ResourceLimitExceeded, ToleranceInvalid
from .serialization import content_checksum


@dataclass(frozen=True)
class ThresholdConfig:
    policy: str = "tolerance"
    registration_limit: float = 0.05
    simplification_min_degree: float = 0.05
    absolute_tolerance: float = 1e-9
    relative_tolerance: float = 1e-12
    protect_subnormal: bool = True

    def __post_init__(self) -> None:
        thresholds.validate_threshold_policy(self.policy, "thresholds.policy")
        thresholds.validate_tolerance(
            self.absolute_tolerance, "thresholds.absolute_tolerance"
        )
        thresholds.validate_tolerance(
            self.relative_tolerance, "thresholds.relative_tolerance"
        )
        for name in ("registration_limit", "simplification_min_degree"):
            value = float(getattr(self, name))
            if not math.isfinite(value):
                raise ToleranceInvalid(field=f"thresholds.{name}", value=value)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class ResourceLimits:
    max_resident_bytes: int = 64 * 1024 * 1024
    max_faces_per_scene: int = 250_000
    max_parallel_recompute: int = 4
    allow_degrade: bool = True

    def __post_init__(self) -> None:
        for name in (
            "max_resident_bytes",
            "max_faces_per_scene",
            "max_parallel_recompute",
        ):
            value = int(getattr(self, name))
            if value <= 0:
                raise ResourceLimitExceeded(resource=name, limit=value, actual=0)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class PipelineConfig:
    grid_rows: int = 9
    grid_cols: int = 9
    icp_max_iterations: int = 30
    icp_tolerance: float = 1e-10
    registration_noise: float = 0.0035
    fusion_strength: float = 0.12
    fusion_sigma_scale: float = 0.035
    texture_height: int = 24
    texture_width: int = 24
    texture_block_height: int = 8
    texture_block_width: int = 8
    camera_image_height: int = 18
    camera_image_width: int = 24
    multi_view_tolerance: float = 0.75
    parallel_workers: int = 1
    thresholds: ThresholdConfig = field(default_factory=ThresholdConfig)
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    def __post_init__(self) -> None:
        if int(self.grid_rows) < 3 or int(self.grid_cols) < 3:
            raise ValueError("grid_rows and grid_cols must both be >= 3")
        if int(self.icp_max_iterations) < 1:
            raise ValueError("icp_max_iterations must be >= 1")
        if float(self.icp_tolerance) < 0 or not math.isfinite(self.icp_tolerance):
            raise ValueError("icp_tolerance must be finite and >= 0")
        if float(self.registration_noise) < 0:
            raise ValueError("registration_noise must be >= 0")
        if not 0.0 <= float(self.fusion_strength) <= 1.0:
            raise ValueError("fusion_strength must be in [0, 1]")
        for name in (
            "texture_height",
            "texture_width",
            "texture_block_height",
            "texture_block_width",
            "camera_image_height",
            "camera_image_width",
            "parallel_workers",
        ):
            if int(getattr(self, name)) <= 0:
                raise ValueError(f"{name} must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid_rows": int(self.grid_rows),
            "grid_cols": int(self.grid_cols),
            "icp_max_iterations": int(self.icp_max_iterations),
            "icp_tolerance": float(self.icp_tolerance),
            "registration_noise": float(self.registration_noise),
            "fusion_strength": float(self.fusion_strength),
            "fusion_sigma_scale": float(self.fusion_sigma_scale),
            "texture_height": int(self.texture_height),
            "texture_width": int(self.texture_width),
            "texture_block_height": int(self.texture_block_height),
            "texture_block_width": int(self.texture_block_width),
            "camera_image_height": int(self.camera_image_height),
            "camera_image_width": int(self.camera_image_width),
            "multi_view_tolerance": float(self.multi_view_tolerance),
            "parallel_workers": int(self.parallel_workers),
            "thresholds": self.thresholds.to_dict(),
            "limits": self.limits.to_dict(),
        }

    @property
    def digest(self) -> str:
        return content_checksum(self.to_dict())

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PipelineConfig":
        data = dict(value)
        thresholds_data = dict(data.pop("thresholds", {}))
        limits_data = dict(data.pop("limits", {}))
        return cls(
            **data,
            thresholds=ThresholdConfig(**thresholds_data),
            limits=ResourceLimits(**limits_data),
        )

    @classmethod
    def from_json_file(cls, path: str) -> "PipelineConfig":
        with open(path, "r", encoding="utf-8") as handle:
            value = json.load(handle)
        return cls.from_dict(value)
