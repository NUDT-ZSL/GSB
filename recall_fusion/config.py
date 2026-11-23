"""Fusion pipeline configuration with deterministic defaults and JSON I/O."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .errors import ErrorCode, PipelineError
from .models import CHANNELS

MERGE_POLICIES = ("highest_normalized", "first_hit")
NORMALIZATIONS = ("scale", "rank_reciprocal")
CONFLICT_METRICS = ("precision", "memory", "latency")


@dataclass
class ChannelConfig:
    enabled: bool = True
    candidate_limit: int = 20
    weight: float = 1.0
    normalization: str | None = None


@dataclass
class BudgetConfig:
    total_candidates: int = 10_000
    fusion_comparisons: int = 5_000
    resident_memory_bytes: int = 1_000_000


@dataclass
class SchedulerConfig:
    workers: int = 2
    batch_size: int = 4
    merge_policy: str = "highest_normalized"
    default_normalization: str = "scale"
    rank_constant: int = 60
    eviction_policy: str = "shard_lru"
    conflict_priority: list[str] = field(
        default_factory=lambda: ["memory", "latency", "precision"]
    )
    two_stage_enabled: bool = False
    fine_pool_size: int = 10
    min_channel_floor: int = 2
    expansion_step: int = 5


@dataclass
class FusionConfig:
    channels: dict[str, ChannelConfig]
    budgets: BudgetConfig
    scheduler: SchedulerConfig
    seed: int = 20261124

    @staticmethod
    def default(overrides: dict | None = None) -> "FusionConfig":
        channels = {name: ChannelConfig() for name in CHANNELS}
        cfg = FusionConfig(
            channels=channels,
            budgets=BudgetConfig(),
            scheduler=SchedulerConfig(),
        )
        if overrides:
            cfg.apply_overrides(overrides)
        return cfg

    def apply_overrides(self, overrides: dict) -> None:
        for key, value in overrides.items():
            if key == "channels" and isinstance(value, dict):
                for channel, patch in value.items():
                    if channel not in self.channels:
                        raise PipelineError(
                            ErrorCode.E_VALIDATION, "config",
                            f"unknown channel {channel!r}", field="channels",
                        )
                    for attr, attr_value in patch.items():
                        setattr(self.channels[channel], attr, attr_value)
            elif key == "budgets" and isinstance(value, dict):
                for attr, attr_value in value.items():
                    setattr(self.budgets, attr, attr_value)
            elif key == "scheduler" and isinstance(value, dict):
                for attr, attr_value in value.items():
                    setattr(self.scheduler, attr, attr_value)
            else:
                setattr(self, key, value)

    def enabled_channels(self) -> list[str]:
        return [name for name in CHANNELS if self.channels[name].enabled]

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )

    @staticmethod
    def load(path: str | Path) -> "FusionConfig":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        channels = {
            name: ChannelConfig(**raw["channels"][name]) for name in CHANNELS
        }
        budgets = BudgetConfig(**raw["budgets"])
        scheduler = SchedulerConfig(**raw["scheduler"])
        return FusionConfig(
            channels=channels,
            budgets=budgets,
            scheduler=scheduler,
            seed=raw.get("seed", 20261124),
        )
