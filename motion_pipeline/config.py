"""Configuration and resource policy."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import BusinessError


SUPPORTED_TEMPORAL_POLICIES = {
    "every_frame": 1,
    "stride_2": 2,
    "stride_3": 3,
}

SUPPORTED_MOTION_MODES = {"constant", "accelerated", "periodic"}

FRAME_HEIGHT = 32
FRAME_WIDTH = 32


@dataclass(frozen=True)
class PipelineConfig:
    """Pipeline settings.

    ``history_window_length`` and ``decay`` affect stage 3 only. Resource
    limits are checked before any stage payload is generated. Parallel
    degradation changes scheduling, never serialized stage bytes.
    """

    history_window_length: int = 6
    decay: float = 0.85
    observation_ratio: float = 0.62
    max_frames_per_clip: int = 96
    max_resident_history_windows: int = 8
    max_parallel_clips: int = 4
    max_history_window_length: int = 64
    resource_policy: str = "degrade"
    hit_distance_px: float = 2.5
    schema_version: int = 1

    def validate(self) -> list[dict[str, Any]]:
        """Return non-fatal degradation markers or raise a business error."""

        if (
            not isinstance(self.observation_ratio, (int, float))
            or isinstance(self.observation_ratio, bool)
            or not math.isfinite(float(self.observation_ratio))
            or not 0.0 < float(self.observation_ratio) < 1.0
        ):
            raise BusinessError(
                "E_CONFIG_INVALID",
                "observation ratio must be strictly between 0 and 1",
                field="observation_ratio",
            )
        for name, minimum in (
            ("max_frames_per_clip", 1),
            ("max_resident_history_windows", 1),
            ("max_parallel_clips", 1),
            ("max_history_window_length", 1),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < minimum
            ):
                raise BusinessError(
                    "E_CONFIG_INVALID",
                    f"{name} must be an integer >= {minimum}",
                    field=name,
                )
        if self.resource_policy not in {"error", "degrade"}:
            raise BusinessError(
                "E_CONFIG_INVALID",
                "resource policy must be 'error' or 'degrade'",
                field="resource_policy",
            )
        if (
            not isinstance(self.hit_distance_px, (int, float))
            or isinstance(self.hit_distance_px, bool)
            or not math.isfinite(float(self.hit_distance_px))
            or float(self.hit_distance_px) <= 0.0
        ):
            raise BusinessError(
                "E_CONFIG_INVALID",
                "hit distance must be finite and positive",
                field="hit_distance_px",
            )
        return []

    def validate_temporal_parameters(self, clip_id: str) -> None:
        """Validate window/decay with an explicit clip attribution."""

        if (
            not isinstance(self.history_window_length, int)
            or isinstance(self.history_window_length, bool)
            or self.history_window_length < 1
            or self.history_window_length > self.max_history_window_length
        ):
            raise BusinessError(
                "E_WINDOW_INVALID",
                "history window length is outside the supported range",
                field="history_window_length",
                clip_id=clip_id,
                limit={"min": 1, "max": self.max_history_window_length},
            )
        if (
            not isinstance(self.decay, (int, float))
            or isinstance(self.decay, bool)
            or not math.isfinite(float(self.decay))
            or float(self.decay) <= 0.0
        ):
            raise BusinessError(
                "E_DECAY_INVALID",
                "decay must be a finite positive number",
                field="decay",
                clip_id=clip_id,
            )

    def parallel_degradations(self, clip_count: int) -> list[dict[str, Any]]:
        if clip_count <= self.max_parallel_clips:
            return []
        marker = {
            "category": "parallel_sequential",
            "requested_parallel_clips": clip_count,
            "effective_parallel_clips": 1,
            "limit": self.max_parallel_clips,
        }
        if self.resource_policy == "error":
            raise BusinessError(
                "E_RESOURCE_LIMIT",
                "parallel clip count exceeds the configured limit",
                field="parallel_clip_count",
                limit=self.max_parallel_clips,
                detail={"requested": clip_count},
            )
        return [marker]

    def as_dict(self) -> dict[str, Any]:
        return {
            "history_window_length": self.history_window_length,
            "decay": float(self.decay),
            "observation_ratio": float(self.observation_ratio),
            "max_frames_per_clip": self.max_frames_per_clip,
            "max_resident_history_windows": self.max_resident_history_windows,
            "max_parallel_clips": self.max_parallel_clips,
            "max_history_window_length": self.max_history_window_length,
            "resource_policy": self.resource_policy,
            "hit_distance_px": float(self.hit_distance_px),
            "schema_version": self.schema_version,
        }
