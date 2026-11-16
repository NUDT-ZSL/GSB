"""Typed VAD configuration with hash-based version identity."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .util import hash_json, load_json

_REQUIRED = (
    "frame_length_ms", "frame_hop_ms", "feature_decimals",
    "energy_high_threshold", "hysteresis", "zcr_threshold",
    "flatness_threshold", "suspect_frames", "trailing_frames",
    "min_speech_ms", "min_silence_ms", "noise_floor_window_frames",
    "max_workers", "snapshot_version",
)


@dataclass(frozen=True)
class VadConfig:
    raw: dict
    frame_length_ms: int
    frame_hop_ms: int
    feature_decimals: int
    energy_high_threshold: float
    hysteresis: float
    zcr_threshold: float
    flatness_threshold: float
    suspect_frames: int
    trailing_frames: int
    min_speech_ms: int
    min_silence_ms: int
    noise_floor_window_frames: int
    max_workers: int
    snapshot_version: int
    config_hash: str

    @classmethod
    def load(cls, path: Path) -> "VadConfig":
        raw = load_json(Path(path))
        missing = [key for key in _REQUIRED if key not in raw]
        if missing:
            raise ValueError(f"missing config keys: {missing}")
        if raw["frame_hop_ms"] <= 0 or raw["frame_length_ms"] < raw["frame_hop_ms"]:
            raise ValueError("frame length must be >= positive frame hop")
        if raw["max_workers"] < 1:
            raise ValueError("max_workers must be >= 1")
        return cls(
            raw=raw,
            frame_length_ms=int(raw["frame_length_ms"]),
            frame_hop_ms=int(raw["frame_hop_ms"]),
            feature_decimals=int(raw["feature_decimals"]),
            energy_high_threshold=float(raw["energy_high_threshold"]),
            hysteresis=float(raw["hysteresis"]),
            zcr_threshold=float(raw["zcr_threshold"]),
            flatness_threshold=float(raw["flatness_threshold"]),
            suspect_frames=int(raw["suspect_frames"]),
            trailing_frames=int(raw["trailing_frames"]),
            min_speech_ms=int(raw["min_speech_ms"]),
            min_silence_ms=int(raw["min_silence_ms"]),
            noise_floor_window_frames=int(raw["noise_floor_window_frames"]),
            max_workers=int(raw["max_workers"]),
            snapshot_version=int(raw["snapshot_version"]),
            config_hash=hash_json(raw),
        )
