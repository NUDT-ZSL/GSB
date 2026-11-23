from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class StreamSpec:
    tenant: str
    metric: str
    tags: tuple[tuple[str, str], ...]
    sample_period_ms: int
    sample_bytes: int
    partition: int
    route_hash: str

    @property
    def tags_dict(self) -> dict[str, str]:
        return dict(self.tags)


@dataclass(frozen=True)
class PartitionConfig:
    partition_count: int
    buffer_per_partition: int
    flush_round_limit: int
    round_time_budget_ms: int
    hash_seed: str
    token_cost_per_sample: int = 1


@dataclass(frozen=True)
class TenantQuotaSpec:
    window_limit_bytes: Decimal
    borrow_limit_bytes: Decimal
    allow_borrow: bool


@dataclass(frozen=True)
class QuotaConfig:
    window_ms: int
    fixed_scale: int
    comparison_tolerance_bytes: Decimal
    tenants: dict[str, TenantQuotaSpec]


@dataclass(frozen=True)
class TokenConfig:
    capacity_tokens: int
    refill_rate_per_second: int
    clock_step_ms: int
    fixed_scale: int


@dataclass(frozen=True)
class Configuration:
    streams: tuple[StreamSpec, ...]
    partitions: PartitionConfig
    quotas: QuotaConfig
    tokens: TokenConfig
    config_hash: str
    raw: dict[str, Any] = field(repr=False)

    @property
    def stream_map(self) -> dict[tuple[str, str], StreamSpec]:
        return {(stream.tenant, stream.metric): stream for stream in self.streams}


@dataclass(frozen=True)
class EventSpec:
    event_id: str
    tenant: str
    metric: str
    tags: tuple[tuple[str, str], ...]
    logical_time_ms: int
    sample_seq: int
    sample_bytes: int
    sample_period_ms: int
    trace_id: str | None = None

    @property
    def tags_dict(self) -> dict[str, str]:
        return dict(self.tags)

