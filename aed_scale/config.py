"""Validated configuration objects and sample-exact frame geometry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from numbers import Real
from typing import Any

from .codes import BusinessCode
from .errors import AedError

SAMPLE_RATE = 16_000

FEATURE_NAMES_FULL = (
    "log_energy",
    "zcr",
    "spectral_centroid_hz",
    "spectral_rolloff_hz",
    "spectral_bandwidth_hz",
    "spectral_flatness",
    "spectral_flux",
    "peak_abs",
    "low_band_ratio",
    "high_band_ratio",
)

FEATURE_NAMES_REDUCED = (
    "log_energy",
    "zcr",
    "spectral_centroid_hz",
    "spectral_rolloff_hz",
)


def _invalid(field: str, message: str, *, dialog_id: str | None = None) -> AedError:
    return AedError(
        BusinessCode.E_CONFIG_INVALID,
        message,
        dialog_id=dialog_id,
        field=field,
    )


def _is_real(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(float(value))


def ms_to_samples(ms: float, sample_rate: int, *, field: str, dialog_id: str | None = None) -> int:
    """Convert milliseconds to samples, rejecting inexact geometry.

    A rejection is preferable to silently rounding: all frame boundaries must be
    reproducible from the configured sample rate.
    """

    if not _is_real(ms):
        raise _invalid(field, f"{field} must be a finite number", dialog_id=dialog_id)
    exact = float(ms) * sample_rate / 1000.0
    nearest = int(round(exact))
    if abs(exact - nearest) > 1e-9:
        raise _invalid(
            field,
            f"{field}={ms} does not map to an integer number of samples at {sample_rate} Hz",
            dialog_id=dialog_id,
        )
    return nearest


@dataclass(frozen=True)
class FeatureConfig:
    """Canonical frame lattice and feature extractor configuration."""

    sample_rate: int = SAMPLE_RATE
    frame_ms: float = 20.0
    hop_ms: float = 10.0
    n_fft: int = 512
    rolloff_ratio: float = 0.85
    feature_mode: str = "full"
    eps: float = 1e-10

    @property
    def frame_samples(self) -> int:
        return ms_to_samples(self.frame_ms, self.sample_rate, field="frame_ms")

    @property
    def hop_samples(self) -> int:
        return ms_to_samples(self.hop_ms, self.sample_rate, field="hop_ms")

    @property
    def feature_names(self) -> tuple[str, ...]:
        return FEATURE_NAMES_FULL if self.feature_mode == "full" else FEATURE_NAMES_REDUCED

    @property
    def feature_dimension(self) -> int:
        return len(self.feature_names)

    def with_mode(self, mode: str) -> "FeatureConfig":
        return FeatureConfig(
            sample_rate=self.sample_rate,
            frame_ms=self.frame_ms,
            hop_ms=self.hop_ms,
            n_fft=self.n_fft,
            rolloff_ratio=self.rolloff_ratio,
            feature_mode=mode,
            eps=self.eps,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_rate": self.sample_rate,
            "frame_ms": self.frame_ms,
            "hop_ms": self.hop_ms,
            "n_fft": self.n_fft,
            "rolloff_ratio": self.rolloff_ratio,
            "feature_mode": self.feature_mode,
            "feature_names": list(self.feature_names),
            "frame_samples": self.frame_samples,
            "hop_samples": self.hop_samples,
        }


def validate_feature_config(config: FeatureConfig, *, dialog_id: str | None = None) -> None:
    if isinstance(config, bool) or not isinstance(config, FeatureConfig):
        raise _invalid("config", "config must be a FeatureConfig instance", dialog_id=dialog_id)
    if isinstance(config.sample_rate, bool) or not isinstance(config.sample_rate, int):
        raise _invalid("sample_rate", "sample_rate must be an integer", dialog_id=dialog_id)
    if config.sample_rate != SAMPLE_RATE:
        raise _invalid(
            "sample_rate",
            f"sample_rate must be {SAMPLE_RATE}, got {config.sample_rate}",
            dialog_id=dialog_id,
        )
    if not _is_real(config.frame_ms) or float(config.frame_ms) <= 0:
        raise _invalid("frame_ms", "frame_ms must be a positive finite number", dialog_id=dialog_id)
    if not _is_real(config.hop_ms) or float(config.hop_ms) <= 0:
        raise _invalid("hop_ms", "hop_ms must be a positive finite number", dialog_id=dialog_id)
    frame_samples = ms_to_samples(config.frame_ms, config.sample_rate, field="frame_ms", dialog_id=dialog_id)
    hop_samples = ms_to_samples(config.hop_ms, config.sample_rate, field="hop_ms", dialog_id=dialog_id)
    if frame_samples < 2:
        raise _invalid("frame_ms", "frame_ms must span at least two samples", dialog_id=dialog_id)
    if hop_samples < 1:
        raise _invalid("hop_ms", "hop_ms must span at least one sample", dialog_id=dialog_id)
    if hop_samples > frame_samples:
        raise _invalid("hop_ms", "hop_ms must not exceed frame_ms", dialog_id=dialog_id)
    if isinstance(config.n_fft, bool) or not isinstance(config.n_fft, int) or config.n_fft <= 0:
        raise _invalid("n_fft", "n_fft must be a positive integer", dialog_id=dialog_id)
    if config.n_fft < frame_samples:
        raise _invalid("n_fft", "n_fft must be >= the frame sample count", dialog_id=dialog_id)
    if config.n_fft & (config.n_fft - 1):
        raise _invalid("n_fft", "n_fft must be a power of two", dialog_id=dialog_id)
    if not _is_real(config.rolloff_ratio) or not 0 < float(config.rolloff_ratio) < 1:
        raise _invalid("rolloff_ratio", "rolloff_ratio must be in (0, 1)", dialog_id=dialog_id)
    if config.feature_mode not in {"full", "reduced"}:
        raise _invalid("feature_mode", "feature_mode must be 'full' or 'reduced'", dialog_id=dialog_id)
    if not _is_real(config.eps) or float(config.eps) <= 0:
        raise _invalid("eps", "eps must be a positive finite number", dialog_id=dialog_id)


@dataclass(frozen=True)
class DetectionConfig:
    """Thresholds and post-processing geometry shared by all tuning paths."""

    min_event_ms: float = 80.0
    min_gap_ms: float = 150.0
    overlap_threshold: float = 0.5
    fine_threshold: float = 0.60
    approximate_threshold: float = 0.74
    fine_passes: int = 3

    def to_dict(self) -> dict[str, Any]:
        return {
            "min_event_ms": self.min_event_ms,
            "min_gap_ms": self.min_gap_ms,
            "overlap_threshold": self.overlap_threshold,
            "fine_threshold": self.fine_threshold,
            "approximate_threshold": self.approximate_threshold,
            "fine_passes": self.fine_passes,
        }


def validate_detection_config(config: DetectionConfig) -> None:
    if not _is_real(config.min_event_ms) or float(config.min_event_ms) < 0:
        raise _invalid("min_event_ms", "min_event_ms must be non-negative")
    if not _is_real(config.min_gap_ms) or float(config.min_gap_ms) < 0:
        raise _invalid("min_gap_ms", "min_gap_ms must be non-negative")
    for field in ("overlap_threshold", "fine_threshold", "approximate_threshold"):
        value = getattr(config, field)
        if not _is_real(value) or not 0 <= float(value) <= 1:
            raise _invalid(field, f"{field} must be in [0, 1]")
    if isinstance(config.fine_passes, bool) or not isinstance(config.fine_passes, int) or config.fine_passes < 1:
        raise _invalid("fine_passes", "fine_passes must be a positive integer")
