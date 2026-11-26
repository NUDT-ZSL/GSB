"""Deterministic configuration defaults and validation."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

from .errors import DiagnosticError, ErrorCode


DEFAULT_CONFIG: Dict[str, Any] = {
    "dialogue_id": "DLG_FIXED_463",
    "seed": 46301314,
    "sample_rate": 16000,
    "frame_ms": 20,
    "hop_ms": 10,
    "duration_s": 14.0,
    "pcm_filename": "dialogue_16k_mono.pcm",
    "truth_filename": "frame_truth.jsonl",
    "workers": 2,
    "batch_frames": 64,
    "max_transitions": 20000,
    "max_feature_memory_bytes": 64 * 1024 * 1024,
    "anomaly": {
        "long_pause_frames": 42,
        "short_interruption_frames": 45,
        "energy_floor": 0.012,
        "energy_sustain_ratio": 0.55,
        "energy_drop_ratio": 0.32,
        "overlap_zcr_delta": 0.055,
    },
    "states": ["neutral", "calm", "happy", "angry", "sad"],
    "default_generation": 1,
    "v2_shard_size": 256,
    "speakers": [
        {
            "speaker_id": 1,
            "name": "SPK01",
            "f0_hz": 126.0,
            "formants_hz": [680.0, 1240.0, 2650.0],
            "energy_level": 0.20,
        },
        {
            "speaker_id": 2,
            "name": "SPK02",
            "f0_hz": 188.0,
            "formants_hz": [760.0, 1450.0, 2900.0],
            "energy_level": 0.18,
        },
        {
            "speaker_id": 3,
            "name": "SPK03",
            "f0_hz": 104.0,
            "formants_hz": [620.0, 1120.0, 2450.0],
            "energy_level": 0.22,
        },
    ],
}


@dataclass(frozen=True)
class EngineConfig:
    values: Dict[str, Any] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_CONFIG)
    )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "EngineConfig":
        values = copy.deepcopy(DEFAULT_CONFIG)
        if path is not None:
            path = Path(path)
            if path.exists():
                user_values = json.loads(path.read_text(encoding="utf-8"))
                _deep_update(values, user_values)
        config = cls(values)
        config.validate()
        return config

    def validate(self) -> None:
        sample_rate = int(self["sample_rate"])
        frame_ms = int(self["frame_ms"])
        hop_ms = int(self["hop_ms"])
        if sample_rate != 16000:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG,
                "only the deterministic 16kHz sample profile is supported",
                {"sample_rate": sample_rate},
            )
        if frame_ms <= 0 or hop_ms <= 0 or hop_ms > frame_ms:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG,
                "frame_ms must be positive and hop_ms must not exceed frame_ms",
                {"frame_ms": frame_ms, "hop_ms": hop_ms},
            )
        if int(self["workers"]) < 1:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG,
                "workers must be >= 1",
                {"workers": self["workers"]},
            )
        if int(self["batch_frames"]) < 1:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG,
                "batch_frames must be >= 1",
                {"batch_frames": self["batch_frames"]},
            )
        if int(self["max_transitions"]) < 1:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG, "max_transitions must be >= 1"
            )
        if int(self["max_feature_memory_bytes"]) < 1:
            raise DiagnosticError(
                ErrorCode.INVALID_CONFIG,
                "max_feature_memory_bytes must be >= 1",
            )

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.values.get(key, default)

    def to_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self.values)

    @property
    def frame_samples(self) -> int:
        return round(int(self["sample_rate"]) * int(self["frame_ms"]) / 1000)

    @property
    def hop_samples(self) -> int:
        return round(int(self["sample_rate"]) * int(self["hop_ms"]) / 1000)


def load_config(path: str | Path | None = None) -> EngineConfig:
    return EngineConfig.load(path)


def _deep_update(base: Dict[str, Any], update: Dict[str, Any]) -> None:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_update(base[key], value)
        else:
            base[key] = value
