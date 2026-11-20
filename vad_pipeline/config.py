"""Pipeline configuration, calibration parameters and validation."""

from __future__ import annotations

from dataclasses import dataclass, asdict

from . import errors

REQUIRED_SAMPLE_RATE = 16000
ALLOWED_FRAME_MS = (5, 10, 20, 25, 30, 40, 50)


@dataclass(frozen=True)
class VadConfig:
    sample_rate: int = REQUIRED_SAMPLE_RATE
    frame_ms: int = 10
    hop_ms: int = 10
    # noise estimation calibration
    percentile: float = 20.0
    percentile_window: int = 25
    # hysteresis threshold calibration
    mult_low: float = 2.0
    mult_high: float = 4.0
    # segment merge calibration
    min_speech_ms: int = 60
    min_silence_ms: int = 40
    # operator-controlled parameter bundle version, part of every cache key
    param_version: str = "v1"
    # resource limits
    max_frames_in_memory: int = 2_000_000
    max_cache_entries: int = 4096
    max_parallel_nodes: int = 4

    def to_dict(self):
        return asdict(self)


def validate_config(cfg: VadConfig) -> None:
    if cfg.sample_rate != REQUIRED_SAMPLE_RATE:
        raise errors.VadError(
            errors.E_SAMPLE_RATE_INVALID,
            "only 16000 Hz mono PCM is supported",
            sample_rate=cfg.sample_rate,
        )
    if cfg.frame_ms not in ALLOWED_FRAME_MS:
        raise errors.VadError(
            errors.E_FRAME_MS_INVALID,
            "frame_ms must be one of " + ", ".join(map(str, ALLOWED_FRAME_MS)),
            frame_ms=cfg.frame_ms,
        )
    if cfg.hop_ms <= 0 or cfg.hop_ms > cfg.frame_ms:
        raise errors.VadError(
            errors.E_HOP_INVALID,
            "hop must be positive and must not exceed the frame length",
            hop_ms=cfg.hop_ms,
            frame_ms=cfg.frame_ms,
        )
    if not (0.0 < cfg.percentile <= 100.0):
        raise errors.VadError(
            errors.E_PERCENTILE_INVALID,
            "percentile must be in (0, 100]",
            percentile=cfg.percentile,
        )
    if cfg.percentile_window < 1:
        raise errors.VadError(
            errors.E_PERCENTILE_INVALID,
            "percentile_window must be >= 1",
            percentile_window=cfg.percentile_window,
        )
    if cfg.mult_low <= 0.0 or cfg.mult_high < cfg.mult_low:
        raise errors.VadError(
            errors.E_MULTIPLIER_INVALID,
            "require 0 < mult_low <= mult_high",
            mult_low=cfg.mult_low,
            mult_high=cfg.mult_high,
        )
    if cfg.min_speech_ms < 0 or cfg.min_silence_ms < 0:
        raise errors.VadError(
            errors.E_MULTIPLIER_INVALID,
            "segment durations must be non-negative",
            min_speech_ms=cfg.min_speech_ms,
            min_silence_ms=cfg.min_silence_ms,
        )
