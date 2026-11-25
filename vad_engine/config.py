"""Engine configuration with explicit resource and numerical policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .errors import E_PARSE_ERROR, EngineError
from .util import object_hash


@dataclass(frozen=True)
class EngineConfig:
    sample_rate: int = 16_000
    frame_length_ms: int = 20
    frame_shift_ms: int = 10
    truth_frame_ms: int = 20
    drift_abs_tolerance: float = 0.018
    drift_rel_tolerance: float = 0.22
    fixed_scale: int = 1_000_000
    max_envelope_memory_bytes: int = 64 * 1024 * 1024
    max_cache_entries: int = 20_000
    max_parallel_chunks: int = 4
    parallel_chunks: int = 1
    strict_resources: bool = False

    def validate(self) -> None:
        if self.sample_rate <= 0:
            raise EngineError(E_PARSE_ERROR, "sample_rate must be positive")
        for name in ("frame_length_ms", "frame_shift_ms", "truth_frame_ms"):
            if getattr(self, name) <= 0:
                raise EngineError(E_PARSE_ERROR, f"{name} must be positive", {"field": name})
        if self.frame_shift_ms > self.frame_length_ms:
            raise EngineError(
                E_PARSE_ERROR,
                "frame_shift_ms cannot exceed frame_length_ms",
            )
        if self.sample_rate * self.frame_length_ms % 1000:
            raise EngineError(
                E_PARSE_ERROR,
                "frame_length_ms does not map to an integer sample count",
            )
        if self.sample_rate * self.frame_shift_ms % 1000:
            raise EngineError(
                E_PARSE_ERROR,
                "frame_shift_ms does not map to an integer sample count",
            )
        if self.drift_abs_tolerance < 0 or self.drift_rel_tolerance < 0:
            raise EngineError(E_PARSE_ERROR, "drift tolerances must be non-negative")
        for name in (
            "max_envelope_memory_bytes",
            "max_cache_entries",
            "max_parallel_chunks",
        ):
            if getattr(self, name) < 1:
                raise EngineError(E_PARSE_ERROR, f"{name} must be at least 1", {"field": name})
        if self.fixed_scale < 1:
            raise EngineError(E_PARSE_ERROR, "fixed_scale must be at least 1")

    @property
    def frame_length_samples(self) -> int:
        return self.sample_rate * self.frame_length_ms // 1000

    @property
    def frame_shift_samples(self) -> int:
        return self.sample_rate * self.frame_shift_ms // 1000

    @property
    def truth_frame_samples(self) -> int:
        return self.sample_rate * self.truth_frame_ms // 1000

    def frame_key(self) -> dict[str, int]:
        return {
            "sample_rate": self.sample_rate,
            "frame_length_samples": self.frame_length_samples,
            "frame_shift_samples": self.frame_shift_samples,
            "frame_length_ms": self.frame_length_ms,
            "frame_shift_ms": self.frame_shift_ms,
        }

    def fingerprint(self) -> str:
        payload = asdict(self)
        payload["frame_length_samples"] = self.frame_length_samples
        payload["frame_shift_samples"] = self.frame_shift_samples
        return object_hash(payload)

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["frame_length_samples"] = self.frame_length_samples
        payload["frame_shift_samples"] = self.frame_shift_samples
        payload["truth_frame_samples"] = self.truth_frame_samples
        return payload
