from __future__ import annotations

import json
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any


@dataclass
class Config:
    """Analysis parameters. All times are seconds and frequencies are hertz."""

    frame_length_s: float = 0.040
    hop_length_s: float = 0.010
    fmin_hz: float = 70.0
    fmax_hz: float = 1000.0
    silence_dbfs: float = -48.0
    voicing_threshold: float = 0.34
    low_band_split_hz: float = 220.0
    high_band_split_hz: float = 330.0
    octave_tolerance_semitones: float = 1.25
    min_note_duration_s: float = 0.11
    max_note_gap_s: float = 0.045
    note_pitch_jump_semitones: float = 1.15
    note_min_confidence: float = 0.28
    note_boundary_window_s: float = 0.045
    energy_dip_db: float = 6.0
    max_consonant_duration_s: float = 0.32
    min_consonant_duration_s: float = 0.025
    pitch_hit_cents: float = 50.0
    boundary_tolerance_s: float = 0.100
    phoneme_boundary_tolerance_s: float = 0.100
    low_confidence_voicing: float = 0.42
    low_confidence_phoneme: float = 0.45
    low_confidence_note: float = 0.42

    def validate(self) -> None:
        if self.frame_length_s <= 0 or self.hop_length_s <= 0:
            raise ValueError("frame_length_s and hop_length_s must be positive")
        if self.fmin_hz >= self.fmax_hz:
            raise ValueError("fmin_hz must be lower than fmax_hz")
        if self.hop_length_s > self.frame_length_s:
            raise ValueError("hop_length_s must not exceed frame_length_s")
        if not 0 < self.pitch_hit_cents < 100:
            raise ValueError("pitch_hit_cents must be in (0, 100)")

    @classmethod
    def from_dict(cls, values: dict[str, Any] | None) -> "Config":
        values = values or {}
        known = {field.name for field in fields(cls)}
        unknown = sorted(set(values) - known)
        if unknown:
            raise ValueError(f"unknown configuration keys: {', '.join(unknown)}")
        config = cls(**values)
        config.validate()
        return config

    @classmethod
    def from_json(cls, path: str | Path | None) -> "Config":
        if path is None:
            config = cls()
            config.validate()
            return config
        with Path(path).open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError("configuration JSON must contain an object")
        return cls.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

