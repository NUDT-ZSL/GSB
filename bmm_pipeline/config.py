from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass


SUBNORMAL_MODES = ("preserve", "flush")
ROUNDING_MODES = ("nearest", "up", "down", "toward_zero")


@dataclass(frozen=True)
class PipelineConfig:
    atol: float = 1e-10
    rtol: float = 1e-10
    max_stage_jobs: int = 10_000
    max_block_count: int = 200_000
    max_accum_bytes: int = 64 * 1024 * 1024
    subnormal_mode: str = "preserve"
    rounding_mode: str = "nearest"

    def __post_init__(self) -> None:
        if self.atol < 0 or self.rtol < 0:
            raise ValueError("atol and rtol must be non-negative")
        if self.max_stage_jobs <= 0:
            raise ValueError("max_stage_jobs must be positive")
        if self.max_block_count <= 0:
            raise ValueError("max_block_count must be positive")
        if self.max_accum_bytes <= 0:
            raise ValueError("max_accum_bytes must be positive")
        if self.subnormal_mode not in SUBNORMAL_MODES:
            raise ValueError(f"unsupported subnormal_mode: {self.subnormal_mode}")
        if self.rounding_mode not in ROUNDING_MODES:
            raise ValueError(f"unsupported rounding_mode: {self.rounding_mode}")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )

    def fingerprint(self) -> str:
        digest = hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()
        return f"sha256:{digest}"
