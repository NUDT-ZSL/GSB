"""Sampling and quality configuration."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .errors import BusinessCode, VolumeRenderError


@dataclass(frozen=True)
class SamplingConfig:
    """Configuration shared by the CPU reference and GPU render paths.

    ``base_step`` is a maximum sample spacing in world units. Each ray segment
    is divided into an integer number of equal blocks whose width is no larger
    than this value, so the first and last samples remain inside the volume
    bounding box.
    """

    base_step: float = 0.8
    adaptive: bool = False
    max_refinement: int = 2
    gradient_threshold: float = 0.20
    early_termination_threshold: float | None = None
    error_threshold: float | None = None
    srgb: bool = False
    random_seed: int = 0
    interior_float_accumulation: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.base_step) or self.base_step <= 0.0:
            raise VolumeRenderError(
                BusinessCode.E_STEP_TOO_SMALL,
                "base_step must be finite and positive",
                details={"base_step": self.base_step},
            )
        if not 0 <= int(self.max_refinement) <= 8:
            raise VolumeRenderError(
                BusinessCode.E_STEP_TOO_SMALL,
                "max_refinement must be an integer in [0, 8]",
                details={"max_refinement": self.max_refinement},
            )
        if not math.isfinite(self.gradient_threshold) or not 0.0 < self.gradient_threshold <= 1.0:
            raise VolumeRenderError(
                BusinessCode.E_TF_RANGE,
                "gradient_threshold must be finite and in (0, 1]",
                details={"gradient_threshold": self.gradient_threshold},
            )
        if self.early_termination_threshold is not None:
            threshold = float(self.early_termination_threshold)
            if not math.isfinite(threshold) or not 0.0 <= threshold < 1.0:
                raise VolumeRenderError(
                    BusinessCode.E_TF_RANGE,
                    "early_termination_threshold must be in [0, 1)",
                    details={"early_termination_threshold": threshold},
                )
        if self.error_threshold is not None:
            threshold = float(self.error_threshold)
            if not math.isfinite(threshold) or threshold < 0.0:
                raise VolumeRenderError(
                    BusinessCode.E_TF_RANGE,
                    "error_threshold must be finite and non-negative",
                    details={"error_threshold": threshold},
                )
        if not self.interior_float_accumulation:
            raise VolumeRenderError(
                BusinessCode.E_TF_RANGE,
                "float64 interior accumulation is required for deterministic output",
                details={"interior_float_accumulation": False},
            )
        object.__setattr__(self, "base_step", float(self.base_step))
        object.__setattr__(self, "max_refinement", int(self.max_refinement))
        object.__setattr__(self, "gradient_threshold", float(self.gradient_threshold))
        object.__setattr__(self, "random_seed", int(self.random_seed))

    @property
    def finest_step(self) -> float:
        return self.base_step / float(1 << self.max_refinement)

    def metadata(self) -> dict[str, Any]:
        return {
            "base_step_world": self.base_step,
            "adaptive": self.adaptive,
            "max_refinement": self.max_refinement,
            "finest_possible_step_world": self.finest_step,
            "gradient_threshold_normalized": self.gradient_threshold,
            "early_termination_threshold": self.early_termination_threshold,
            "error_threshold": self.error_threshold,
            "srgb": self.srgb,
            "random_seed": self.random_seed,
            "jitter": False,
            "interior_float_accumulation": "float64",
            "reduction_order": "single-threaded deterministic front-to-back sample order",
        }

