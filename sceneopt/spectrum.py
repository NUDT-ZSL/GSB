"""Error propagation spectrum and dominant-source attribution."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable

import numpy as np

from .errors import ErrorSpectrumInvalid

LEVEL_ORDER = (
    "registration",
    "fusion_weights",
    "simplification",
    "texture_block",
)
_TINY = float(np.finfo(np.float64).tiny)


def _finite_nonnegative(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ErrorSpectrumInvalid(field, "value must be numeric") from exc
    if not math.isfinite(number) or number < 0.0:
        raise ErrorSpectrumInvalid(field, "value must be finite and >= 0")
    return number


@dataclass(frozen=True)
class ErrorLevel:
    level: str
    absolute_error: float
    relative_error: float
    error_interval: tuple[float, float]
    amplification_ratio: float
    reference_scale: float

    def __post_init__(self) -> None:
        if self.level not in LEVEL_ORDER:
            raise ErrorSpectrumInvalid("level", f"unknown error level {self.level!r}")
        _finite_nonnegative(self.absolute_error, f"{self.level}.absolute_error")
        _finite_nonnegative(self.relative_error, f"{self.level}.relative_error")
        _finite_nonnegative(
            self.amplification_ratio, f"{self.level}.amplification_ratio"
        )
        _finite_nonnegative(self.reference_scale, f"{self.level}.reference_scale")
        try:
            lower, upper = self.error_interval
        except (TypeError, ValueError) as exc:
            raise ErrorSpectrumInvalid(
                f"{self.level}.error_interval",
                "error interval must contain exactly two values",
            ) from exc
        lower_value = _finite_nonnegative(
            lower, f"{self.level}.error_interval.lower"
        )
        upper_value = _finite_nonnegative(
            upper, f"{self.level}.error_interval.upper"
        )
        if lower_value > upper_value:
            raise ErrorSpectrumInvalid(
                f"{self.level}.error_interval",
                "error interval lower bound exceeds upper bound",
                lower=lower_value,
                upper=upper_value,
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "absolute_error": float(self.absolute_error),
            "relative_error": float(self.relative_error),
            "error_interval": [
                float(self.error_interval[0]),
                float(self.error_interval[1]),
            ],
            "amplification_ratio": float(self.amplification_ratio),
            "reference_scale": float(self.reference_scale),
        }


@dataclass(frozen=True)
class ErrorSpectrum:
    levels: tuple[ErrorLevel, ...]
    dominant_level: str
    reference: str = "float64_full_path"

    def __post_init__(self) -> None:
        levels = tuple(self.levels)
        if tuple(level.level for level in levels) != LEVEL_ORDER:
            raise ErrorSpectrumInvalid(
                "levels",
                f"levels must appear in order {LEVEL_ORDER}",
            )
        for index, level in enumerate(levels):
            prefix = f"levels[{index}].{level.level}"
            _finite_nonnegative(level.absolute_error, f"{prefix}.absolute_error")
            _finite_nonnegative(level.relative_error, f"{prefix}.relative_error")
            _finite_nonnegative(
                level.amplification_ratio, f"{prefix}.amplification_ratio"
            )
            _finite_nonnegative(level.reference_scale, f"{prefix}.reference_scale")
            lower, upper = level.error_interval
            lower_value = _finite_nonnegative(lower, f"{prefix}.error_interval.lower")
            upper_value = _finite_nonnegative(upper, f"{prefix}.error_interval.upper")
            if lower_value > upper_value:
                raise ErrorSpectrumInvalid(
                    f"{prefix}.error_interval",
                    "error interval lower bound exceeds upper bound",
                    lower=lower_value,
                    upper=upper_value,
                )
        if self.dominant_level not in LEVEL_ORDER:
            raise ErrorSpectrumInvalid(
                "dominant_level", "dominant level is not a known stage"
            )
        object.__setattr__(self, "levels", levels)

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "levels": [level.to_dict() for level in self.levels],
            "dominant_level": self.dominant_level,
        }


def _amplification(previous: float, current: float) -> float:
    if current == 0.0:
        return 0.0
    if previous == 0.0:
        # Undefined ratios are neutral rather than non-finite.
        return 1.0
    return float(current / previous)


def build_error_spectrum(
    *,
    registration_absolute_error: float,
    registration_error_interval: tuple[float, float],
    registration_scale: float,
    fusion_absolute_error: float,
    fusion_error_interval: tuple[float, float],
    simplification_absolute_error: float,
    simplification_error_interval: tuple[float, float],
    simplification_scale: float,
    texture_absolute_error: float,
    texture_error_interval: tuple[float, float],
    texture_scale: float,
) -> ErrorSpectrum:
    scales = (
        float(registration_scale),
        float(simplification_scale),
        float(texture_scale),
    )
    if any((not math.isfinite(scale) or scale < 0.0) for scale in scales):
        raise ErrorSpectrumInvalid("reference_scale", "reference scales must be finite and >= 0")

    registration_rel = float(registration_absolute_error) / max(
        float(registration_scale), _TINY
    )
    fusion_rel = float(fusion_absolute_error)
    simplification_rel = float(simplification_absolute_error) / max(
        float(simplification_scale), _TINY
    )
    texture_rel = float(texture_absolute_error) / max(float(texture_scale), _TINY)
    relative_values = (
        registration_rel,
        fusion_rel,
        simplification_rel,
        texture_rel,
    )
    amplification: list[float] = [1.0]
    for previous, current in zip(relative_values[:-1], relative_values[1:]):
        amplification.append(_amplification(previous, current))

    levels = (
        ErrorLevel(
            "registration",
            float(registration_absolute_error),
            registration_rel,
            (float(registration_error_interval[0]), float(registration_error_interval[1])),
            amplification[0],
            float(registration_scale),
        ),
        ErrorLevel(
            "fusion_weights",
            float(fusion_absolute_error),
            fusion_rel,
            (float(fusion_error_interval[0]), float(fusion_error_interval[1])),
            amplification[1],
            1.0,
        ),
        ErrorLevel(
            "simplification",
            float(simplification_absolute_error),
            simplification_rel,
            (
                float(simplification_error_interval[0]),
                float(simplification_error_interval[1]),
            ),
            amplification[2],
            float(simplification_scale),
        ),
        ErrorLevel(
            "texture_block",
            float(texture_absolute_error),
            texture_rel,
            (float(texture_error_interval[0]), float(texture_error_interval[1])),
            amplification[3],
            float(texture_scale),
        ),
    )
    dominant_index = max(
        range(len(levels)), key=lambda index: (levels[index].relative_error, -index)
    )
    return ErrorSpectrum(
        levels=levels,
        dominant_level=levels[dominant_index].level,
        reference="float64_full_path",
    )


def validate_error_spectrum(levels: Iterable[ErrorLevel], dominant_level: str) -> ErrorSpectrum:
    return ErrorSpectrum(tuple(levels), dominant_level)

