"""Explicit configuration objects and deterministic run identifiers."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .canonical import checksum, r6
from .codes import E_INPUT_INVALID
from .errors import BusinessError

PIPELINE_REVISION = "motion-temporal-pipeline-v1.0.0"


@dataclass(slots=True)
class PipelineConfig:
    window_length: int = 4
    decay: float = 0.85
    acceleration_threshold: float = 800.0
    hit_iou_threshold: float = 0.35
    max_sequence_frames: int = 512
    max_history_windows: int = 32
    max_parallel_clips: int = 8
    resource_policy: str = "error"
    extrapolation_margin: float = 96.0

    def __post_init__(self) -> None:
        self.decay = r6(self.decay)
        self.acceleration_threshold = r6(self.acceleration_threshold)
        self.hit_iou_threshold = r6(self.hit_iou_threshold)
        self.extrapolation_margin = r6(self.extrapolation_margin)
        if self.resource_policy not in {"error", "degrade"}:
            raise BusinessError(E_INPUT_INVALID, "resource_policy must be error or degrade")
        if self.max_sequence_frames < 1 or self.max_history_windows < 1 or self.max_parallel_clips < 1:
            raise BusinessError(E_INPUT_INVALID, "resource limits must be positive")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "PipelineConfig":
        return cls(**dict(value or {}))


def parse_model_revision(revision: str) -> int:
    digits = "".join(character for character in str(revision) if character.isdigit())
    if digits:
        return max(1, int(digits))
    return max(1, int(checksum(str(revision))[:8], 16))


def make_run_id(
    spec: Any,
    config: PipelineConfig,
    injections: Iterable[dict[str, Any]] = (),
) -> str:
    from .canonical import canonical_bytes, sha256_bytes

    payload = {
        "pipeline_revision": PIPELINE_REVISION,
        "spec": spec.canonical_dict(),
        "config": config.to_dict(),
        "injections": list(injections),
    }
    return sha256_bytes(canonical_bytes(payload))[:24]
