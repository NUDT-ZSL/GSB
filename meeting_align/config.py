"""Explicit alignment + resource configuration."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ResourceLimits:
    max_frame_feature_bytes: int = 4_000_000
    max_cache_entries: int = 512
    max_seam_candidates: int = 256
    # "degrade" -> fall back to a restricted (doubled) frame hop and mark the
    # degradation category; "strict" -> raise E_RESOURCE_LIMIT.
    on_frame_memory_exceeded: str = "degrade"

    def to_dict(self):
        return asdict(self)


@dataclass
class AlignConfig:
    sample_rate: int = 16000
    frame_len: int = 400          # samples, 25 ms @ 16 kHz
    frame_hop: int = 160          # samples, 10 ms @ 16 kHz
    gap_energy_threshold: float = 1e-4
    seam_energy_threshold: float = 0.02
    seam_zcr_threshold: float = 0.10
    splice_silence_window_frames: int = 6
    max_seam_candidates: int = 64
    conflict_strategy: str = "truncate"   # "truncate" | "shift"
    rate_tolerance: float = 0.01          # declared-vs-applied relative tolerance
    match_tolerance_ms: float = 40.0
    min_segment_ms: float = 50.0
    confidence_decay_per_ms: float = 5e-4
    resource: ResourceLimits = field(default_factory=ResourceLimits)

    def to_dict(self):
        return asdict(self)

    def save(self, path):
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def from_dict(cls, data):
        data = dict(data)
        res = data.pop("resource", {})
        cfg = cls(**data)
        cfg.resource = ResourceLimits(**res)
        return cfg

    @classmethod
    def load(cls, path):
        return cls.from_dict(json.loads(Path(path).read_text()))
