"""Pipeline configuration, validation chain and resource limits."""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field

from .errors import AEDError, Code

SAMPLE_RATE = 16000
KNOWN_TEMPLATE_VERSIONS = ("t1", "t2", "t3")
FEATURE_VERSION = "feat-v1"


@dataclass(frozen=True)
class PipelineConfig:
    sample_rate: int = SAMPLE_RATE
    frame_len: int = 320          # 20 ms @ 16 kHz
    hop: int = 160                # 10 ms
    min_event_ms: int = 60
    min_gap_ms: int = 40
    template_version: str = "t1"
    score_threshold: float = 0.5
    # resource limits (configurable)
    max_frames_resident: int = 200_000
    max_cache_entries: int = 1_000
    max_parallel_recompute: int = 4

    def validate(self) -> "PipelineConfig":
        if self.sample_rate != SAMPLE_RATE:
            raise AEDError(Code.E_SAMPLE_RATE_INVALID,
                           "sample rate must be 16000",
                           sample_rate=self.sample_rate)
        if self.hop > self.frame_len:
            raise AEDError(Code.E_HOP_EXCEEDS_FRAME,
                           "hop must not exceed frame length",
                           hop=self.hop, frame_len=self.frame_len)
        if self.template_version not in KNOWN_TEMPLATE_VERSIONS:
            raise AEDError(Code.E_TEMPLATE_VERSION_UNKNOWN,
                           "unknown template library version",
                           template_version=self.template_version)
        if self.frame_len <= 0 or self.hop <= 0:
            raise AEDError(Code.E_RESOURCE_LIMIT,
                           "frame_len/hop must be positive")
        return self

    @property
    def frame_ms(self) -> float:
        return self.frame_len * 1000.0 / self.sample_rate

    @property
    def hop_ms(self) -> float:
        return self.hop * 1000.0 / self.sample_rate

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "PipelineConfig":
        return PipelineConfig(**{k: v for k, v in d.items()
                                 if k in PipelineConfig.__dataclass_fields__})

    @staticmethod
    def load(path: str) -> "PipelineConfig":
        with open(path, "r", encoding="utf-8") as fh:
            return PipelineConfig.from_dict(json.load(fh)).validate()

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, indent=2, sort_keys=True)
