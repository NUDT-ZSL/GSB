"""Pipeline configuration loading and strict validation."""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any

from .determinism import content_hash, read_json
from .errors import ConfigError

REQUIRED_FIELDS = {
    "sample_rate": int,
    "channels": int,
    "subtype": str,
    "frame_ms": (int, float),
    "hop_ms": (int, float),
    "voiceprint_dims": int,
    "quant_levels": int,
    "energy_db_floor": (int, float),
    "flatness_high": (int, float),
    "flatness_low": (int, float),
    "overlap_similarity": (int, float),
    "overlap_distance_gate": (int, float),
    "cluster_distance_threshold": (int, float),
    "min_speech_ms": (int, float),
    "min_silence_ms": (int, float),
    "max_workers": int,
    "interact_budget_ms": (int, float),
    "snapshot_interval": int,
}

ALLOWED_SUBTYPES = {"PCM_16"}
ALLOWED_QUANT_LEVELS = {4, 8}


@dataclass(frozen=True)
class PipelineConfig:
    sample_rate: int
    channels: int
    subtype: str
    frame_ms: float
    hop_ms: float
    voiceprint_dims: int
    quant_levels: int
    energy_db_floor: float
    flatness_high: float
    flatness_low: float
    overlap_similarity: float
    overlap_distance_gate: float
    cluster_distance_threshold: float
    min_speech_ms: float
    min_silence_ms: float
    max_workers: int
    interact_budget_ms: float
    snapshot_interval: int

    @property
    def frame_size(self) -> int:
        return int(round(self.sample_rate * self.frame_ms / 1000.0))

    @property
    def hop_size(self) -> int:
        return int(round(self.sample_rate * self.hop_ms / 1000.0))

    @property
    def min_speech_frames(self) -> int:
        return max(1, int(round(self.min_speech_ms / self.hop_ms)))

    @property
    def min_silence_frames(self) -> int:
        return max(1, int(round(self.min_silence_ms / self.hop_ms)))

    def with_overrides(self, **overrides: Any) -> "PipelineConfig":
        data = asdict(self)
        data.update(overrides)
        return PipelineConfig(**data)

    def fingerprint(self) -> str:
        return content_hash(asdict(self))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_config(path: str) -> PipelineConfig:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise ConfigError("configuration root must be a JSON object")
    return build_config(raw)


def build_config(raw: dict[str, Any]) -> PipelineConfig:
    missing = sorted(set(REQUIRED_FIELDS) - set(raw))
    if missing:
        raise ConfigError(f"missing config fields: {','.join(missing)}")
    unknown = sorted(set(raw) - set(REQUIRED_FIELDS))
    if unknown:
        raise ConfigError(f"unknown config fields: {','.join(unknown)}")

    cleaned: dict[str, Any] = {}
    for key, expected in REQUIRED_FIELDS.items():
        value = raw[key]
        if isinstance(value, bool) or not isinstance(value, expected):
            raise ConfigError(f"field {key} has wrong type: {value!r}")
        cleaned[key] = value

    if cleaned["sample_rate"] != 16000:
        raise ConfigError("sample_rate must be 16000")
    if cleaned["channels"] != 1:
        raise ConfigError("channels must be 1")
    if cleaned["subtype"] not in ALLOWED_SUBTYPES:
        raise ConfigError("subtype must be PCM_16")
    if cleaned["frame_ms"] <= 0 or cleaned["hop_ms"] <= 0:
        raise ConfigError("frame_ms and hop_ms must be positive")
    if cleaned["hop_ms"] > cleaned["frame_ms"]:
        raise ConfigError("hop_ms must not exceed frame_ms")
    if not 2 <= cleaned["voiceprint_dims"] <= 64:
        raise ConfigError("voiceprint_dims must be within [2, 64]")
    if cleaned["quant_levels"] not in ALLOWED_QUANT_LEVELS:
        raise ConfigError("quant_levels must be 4 or 8")
    if not 0.0 < cleaned["flatness_low"] <= cleaned["flatness_high"] < 1.0:
        raise ConfigError(
            "flatness thresholds require 0 < low(hold) <= high(enter) < 1"
        )
    if not 0.0 <= cleaned["overlap_similarity"] <= 1.0:
        raise ConfigError("overlap_similarity must be within [0, 1]")
    if not 0.0 < cleaned["overlap_distance_gate"] <= 10.0:
        raise ConfigError("overlap_distance_gate must be within (0, 10]")
    if not 0.0 < cleaned["cluster_distance_threshold"] <= 8.0:
        raise ConfigError("cluster_distance_threshold must be within (0, 8]")
    if cleaned["max_workers"] < 1:
        raise ConfigError("max_workers must be >= 1")
    if cleaned["interact_budget_ms"] < 0:
        raise ConfigError("interact_budget_ms must be >= 0")
    if cleaned["snapshot_interval"] < 1:
        raise ConfigError("snapshot_interval must be >= 1")
    return PipelineConfig(**cleaned)
