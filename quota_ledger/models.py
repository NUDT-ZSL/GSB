"""Validated domain models used by the engine and persistence layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from hashlib import sha256
from typing import Any

from .utils import (
    TOKEN_SCALE,
    canonical_json,
    fraction_text,
    layer_sort_key,
    make_path,
)


@dataclass(frozen=True)
class QuotaLayer:
    level: str
    tenant: str
    bucket: str
    prefix: str
    max_bytes: int
    max_objects: int
    soft_pct: int
    hard_pct: int

    @property
    def path(self) -> str:
        return make_path(self.level, self.tenant, self.bucket, self.prefix)

    @property
    def soft_bytes(self) -> int:
        return self.max_bytes * self.soft_pct // 100

    @property
    def soft_objects(self) -> int:
        return self.max_objects * self.soft_pct // 100

    @property
    def hard_bytes(self) -> int:
        return self.max_bytes * self.hard_pct // 100

    @property
    def hard_objects(self) -> int:
        return self.max_objects * self.hard_pct // 100


class QuotaConfig:
    def __init__(self, document: dict[str, Any], layers: list[QuotaLayer]) -> None:
        self.document = document
        self.layers = tuple(sorted(layers, key=layer_sort_key))
        self.by_key = {
            (layer.level, layer.tenant, layer.bucket, layer.prefix): layer
            for layer in self.layers
        }
        self.by_path = {layer.path: layer for layer in self.layers}
        self.tenants = tuple(sorted({layer.tenant for layer in self.layers}))

    def layer(
        self,
        level: str,
        tenant: str,
        bucket: str = "",
        prefix: str = "",
    ) -> QuotaLayer:
        return self.by_key[(level, tenant, bucket, prefix)]

    def event_layers(self, event: "Event") -> tuple[QuotaLayer, QuotaLayer, QuotaLayer]:
        return (
            self.layer("tenant", event.tenant),
            self.layer("bucket", event.tenant, event.bucket),
            self.layer("prefix", event.tenant, event.bucket, event.prefix),
        )

    def has_path(self, tenant: str, bucket: str, prefix: str) -> bool:
        return ("prefix", tenant, bucket, prefix) in self.by_key


@dataclass(frozen=True)
class TenantRateConfig:
    tenant: str
    requests_per_second: int
    bucket_capacity: int
    refill_rate_per_second: Fraction
    burst_depth: int
    backoff_window_ms: int

    @property
    def capacity_tokens(self) -> int:
        return self.bucket_capacity + self.burst_depth

    @property
    def capacity_micro(self) -> int:
        return self.capacity_tokens * TOKEN_SCALE

    @property
    def effective_rate(self) -> Fraction:
        ceiling = Fraction(self.requests_per_second, 1)
        return min(self.refill_rate_per_second, ceiling)

    @property
    def rate_numerator(self) -> int:
        return self.effective_rate.numerator

    @property
    def rate_denominator(self) -> int:
        return self.effective_rate.denominator

    def policy_dict(self) -> dict[str, Any]:
        return {
            "requests_per_second": self.requests_per_second,
            "bucket_capacity": self.bucket_capacity,
            "refill_rate_per_second": fraction_text(self.refill_rate_per_second),
            "effective_refill_rate_per_second": fraction_text(self.effective_rate),
            "burst_depth": self.burst_depth,
            "effective_capacity_tokens": self.capacity_tokens,
            "backoff_window_ms": self.backoff_window_ms,
        }


class LimiterConfig:
    def __init__(
        self,
        document: dict[str, Any],
        tenants: dict[str, TenantRateConfig],
        clock_rollback_tolerance_ms: int,
    ) -> None:
        self.document = document
        self.tenants = tenants
        self.clock_rollback_tolerance_ms = clock_rollback_tolerance_ms

    def tenant(self, name: str) -> TenantRateConfig:
        return self.tenants[name]


@dataclass(frozen=True)
class Event:
    seq: int
    tenant: str
    bucket: str
    prefix: str
    object_key: str
    bytes: int
    deleted: bool
    clock_ms: int
    idempotency_key: str

    def payload(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "tenant": self.tenant,
            "bucket": self.bucket,
            "prefix": self.prefix,
            "object_key": self.object_key,
            "bytes": self.bytes,
            "deleted": self.deleted,
            "clock_ms": self.clock_ms,
            "idempotency_key": self.idempotency_key,
        }

    @property
    def operation(self) -> str:
        return "DELETE" if self.deleted else "PUT"

    @property
    def fingerprint(self) -> str:
        return sha256(canonical_json(self.payload()).encode("utf-8")).hexdigest()


@dataclass
class Usage:
    used_bytes: int = 0
    used_objects: int = 0
    soft_active: bool = False
    soft_entry_seq: int | None = None
    soft_entry_clock_ms: int | None = None
    soft_entry_used_bytes: int | None = None
    soft_entry_used_objects: int | None = None
    soft_entry_soft_bytes: int | None = None
    soft_entry_soft_objects: int | None = None
    soft_exit_seq: int | None = None
    soft_exit_clock_ms: int | None = None
    soft_exit_used_bytes: int | None = None
    soft_exit_used_objects: int | None = None
    soft_exit_soft_bytes: int | None = None
    soft_exit_soft_objects: int | None = None


@dataclass
class ObjectRecord:
    tenant: str
    bucket: str
    prefix: str
    object_key: str
    size_bytes: int
    created_seq: int
    updated_seq: int

    @property
    def identity(self) -> tuple[str, str, str, str]:
        return (self.tenant, self.bucket, self.prefix, self.object_key)


@dataclass
class TokenState:
    tenant: str
    tokens_micro: int
    last_refill_ms: int
    last_seen_ms: int
    refill_remainder_num: int
    capacity_micro: int


@dataclass
class State:
    usage: dict[str, Usage] = field(default_factory=dict)
    objects: dict[tuple[str, str, str, str], ObjectRecord] = field(
        default_factory=dict
    )
    tokens: dict[str, TokenState] = field(default_factory=dict)
    idempotency: dict[str, tuple[str, dict[str, Any]]] = field(default_factory=dict)
    last_decided_seq: int = 0
    decision_results: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def initial(cls, quota: QuotaConfig, limiter: LimiterConfig) -> "State":
        state = cls()
        for layer in quota.layers:
            state.usage[layer.path] = Usage()
        for tenant, policy in limiter.tenants.items():
            state.tokens[tenant] = TokenState(
                tenant=tenant,
                tokens_micro=policy.capacity_micro,
                last_refill_ms=0,
                last_seen_ms=0,
                refill_remainder_num=0,
                capacity_micro=policy.capacity_micro,
            )
        return state

