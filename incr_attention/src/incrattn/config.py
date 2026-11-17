"""Engine configuration and configuration fingerprint."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .errors import InvalidConfigError
from .util import canonical_json, digest_bytes


@dataclass(frozen=True)
class EngineConfig:
    """Static configuration for an incremental attention session.

    block_size: output / KV block size in sequence positions.
    recompute_budget: maximum output blocks recomputed in one batch.
    max_workers: thread-pool size; 1 forces serial execution.
    snapshot_path: destination of the atomic snapshot file.
    scale: QK logit scale; ``None`` means 1 / sqrt(head_dim).
    """

    block_size: int
    recompute_budget: int
    max_workers: int
    snapshot_path: str
    scale: float | None = None

    def validate(self, allow_zero_budget: bool = False) -> None:
        if self.block_size <= 0:
            raise InvalidConfigError(
                f"block_size must be positive, got {self.block_size}",
            )
        if self.recompute_budget < 0:
            raise InvalidConfigError("recompute_budget must be >= 0")
        if self.recompute_budget == 0 and not allow_zero_budget:
            raise InvalidConfigError(
                "recompute_budget=0 is only allowed under explicit injection"
            )
        if self.max_workers <= 0:
            raise InvalidConfigError("max_workers must be positive")
        if not self.snapshot_path:
            raise InvalidConfigError("snapshot_path is required")
        snapshot_dir = os.path.dirname(os.path.abspath(self.snapshot_path))
        os.makedirs(snapshot_dir, exist_ok=True)

    def fingerprint(self) -> str:
        payload = {
            "block_size": self.block_size,
            "recompute_budget": self.recompute_budget,
            "max_workers": self.max_workers,
            "scale": self.scale,
            "causal": True,
            "version": 1,
        }
        return digest_bytes([canonical_json(payload)])

    def replace(self, **changes) -> "EngineConfig":
        from dataclasses import replace

        return replace(self, **changes)


def load_config(path: str) -> EngineConfig:
    import json

    with open(path, "r", encoding="utf-8") as handle:
        raw = json.load(handle)
    config = EngineConfig(
        block_size=int(raw["block_size"]),
        recompute_budget=int(raw["recompute_budget"]),
        max_workers=int(raw["max_workers"]),
        snapshot_path=raw["snapshot_path"],
        scale=(float(raw["scale"]) if raw.get("scale") is not None else None),
    )
    config.validate()
    return config
