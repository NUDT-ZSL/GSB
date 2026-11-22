from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class Policy:
    logical_clock: int
    validation_cycle_ticks: int
    freshness_tolerance_ticks: int
    clock_rollback_tolerance_ticks: int
    origin_retry_budget: int
    origin_request_budget_ms: int
    logical_clock_step: int
    origin_seconds_per_tick: int
    comparison_threshold: int
    sample_fraction: float
    sample_seed: str
    origin_epoch_utc: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_clock": self.logical_clock,
            "validation_cycle_ticks": self.validation_cycle_ticks,
            "freshness_tolerance_ticks": self.freshness_tolerance_ticks,
            "clock_rollback_tolerance_ticks": self.clock_rollback_tolerance_ticks,
            "origin_retry_budget": self.origin_retry_budget,
            "origin_request_budget_ms": self.origin_request_budget_ms,
            "logical_clock_step": self.logical_clock_step,
            "origin_seconds_per_tick": self.origin_seconds_per_tick,
            "comparison_threshold": self.comparison_threshold,
            "sample_fraction": self.sample_fraction,
            "sample_seed": self.sample_seed,
            "origin_epoch_utc": self.origin_epoch_utc,
        }


@dataclass(frozen=True)
class NodeConfig:
    name: str
    region: str
    capacity: int
    validation_budget: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "region": self.region,
            "capacity": self.capacity,
            "validation_budget": self.validation_budget,
        }


@dataclass(frozen=True)
class CacheControl:
    max_age: int | None
    s_maxage: int | None
    effective_ttl_seconds: int
    cacheable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_age": self.max_age,
            "s_maxage": self.s_maxage,
            "effective_ttl_seconds": self.effective_ttl_seconds,
            "cacheable": self.cacheable,
        }


@dataclass(frozen=True)
class SourceResource:
    path: str
    body: bytes
    content_version: int
    etag: str
    digest_algorithm: str
    content_digest: str
    cache_control: CacheControl
    cacheable: bool
    last_modified_clock: int
    weak_etag: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "content_version": self.content_version,
            "etag": self.etag,
            "digest_algorithm": self.digest_algorithm,
            "content_digest": self.content_digest,
            "cache_control": self.cache_control.to_dict(),
            "cacheable": self.cacheable,
            "last_modified_clock": self.last_modified_clock,
            "weak_etag": self.weak_etag,
            "body_sha256_for_trace": self.content_digest,
        }


@dataclass(frozen=True)
class Injection:
    seq: int
    stage: str
    type: str
    path: str
    node: str | None = None
    rollback_ticks: int = 0
    delay_ms: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "stage": self.stage,
            "type": self.type,
            "path": self.path,
            "node": self.node,
            "rollback_ticks": self.rollback_ticks,
            "delay_ms": self.delay_ms,
            "details": self.details,
        }


@dataclass(frozen=True)
class CacheEntry:
    node: str
    path: str
    digest_algorithm: str
    content_digest: str
    content_version: int
    etag: str
    last_verified_clock: int
    expires_at_clock: int
    cache_generation: int
    source_version: str
    source_ref: str
    raw_time: dict[str, Any]
    effective_ttl_seconds: int
    cacheable: bool = True
    weak_etag: bool = False

    @property
    def key(self) -> tuple[str, str]:
        return self.node, self.path

    @property
    def key_text(self) -> str:
        return f"{self.node}:{self.path}"

    def with_changes(self, **changes: Any) -> "CacheEntry":
        return replace(self, **changes)

    def candidate_dict(self) -> dict[str, Any]:
        return {
            "node": self.node,
            "path": self.path,
            "digest_algorithm": self.digest_algorithm,
            "content_digest": self.content_digest,
            "content_version": self.content_version,
            "etag": self.etag,
            "last_verified_clock": self.last_verified_clock,
            "expires_at_clock": self.expires_at_clock,
            "cache_generation": self.cache_generation,
            "source_version": self.source_version,
            "source_ref": self.source_ref,
            "raw_time": self.raw_time,
            "effective_ttl_seconds": self.effective_ttl_seconds,
            "cacheable": self.cacheable,
            "weak_etag": self.weak_etag,
        }

    def row(self) -> tuple[Any, ...]:
        import json

        return (
            self.node,
            self.path,
            self.digest_algorithm,
            self.content_digest,
            self.content_version,
            self.etag,
            self.last_verified_clock,
            self.expires_at_clock,
            self.cache_generation,
            self.source_version,
            self.source_ref,
            json.dumps(self.raw_time, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            self.effective_ttl_seconds,
            1 if self.cacheable else 0,
            1 if self.weak_etag else 0,
        )

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "CacheEntry":
        return cls(
            node=str(value["node"]),
            path=str(value["path"]),
            digest_algorithm=str(value["digest_algorithm"]),
            content_digest=str(value["content_digest"]),
            content_version=int(value["content_version"]),
            etag=str(value["etag"]),
            last_verified_clock=int(value["last_verified_clock"]),
            expires_at_clock=int(value["expires_at_clock"]),
            cache_generation=int(value["cache_generation"]),
            source_version=str(value["source_version"]),
            source_ref=str(value["source_ref"]),
            raw_time=dict(value["raw_time"]),
            effective_ttl_seconds=int(value["effective_ttl_seconds"]),
            cacheable=bool(value["cacheable"]),
            weak_etag=bool(value.get("weak_etag", False)),
        )


@dataclass(frozen=True)
class FreshnessResult:
    status: str
    remaining_ticks: int
    tolerance_ticks: int
    evaluated_at_clock: int
    expires_at_clock: int
    conversion: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "remaining_ticks": self.remaining_ticks,
            "tolerance_ticks": self.tolerance_ticks,
            "evaluated_at_clock": self.evaluated_at_clock,
            "expires_at_clock": self.expires_at_clock,
            "conversion": self.conversion,
        }


@dataclass(frozen=True)
class OriginResponse:
    status: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: float
    attempts: int
    retry_exhausted: bool = False


@dataclass
class ValidatedSamples:
    samples_dir: str
    policy: Policy
    nodes: dict[str, NodeConfig]
    resources: dict[str, SourceResource]
    injections: list[Injection]
    snapshot_candidates: list[CacheEntry]
    snapshot_arbitrations: list[dict[str, Any]]
    snapshot_generation: int
    v1_generation: int
    v2_generation: int
    source_files: dict[str, str]

