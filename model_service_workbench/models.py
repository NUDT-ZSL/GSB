"""Canonical data objects shared by estimation, scheduling and persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ServiceSpec:
    name: str
    requests: tuple[int, ...]
    concurrency_per_replica: int
    request_latency_ms: int
    scale_threshold: float
    min_replicas: int
    max_replicas: int
    concurrency_quota: int

    @property
    def allocation_cap(self) -> int:
        """Replica allocation cap; quotas use replica units in this workbench."""

        return min(self.max_replicas, self.concurrency_quota)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "requests": list(self.requests),
            "concurrency_per_replica": self.concurrency_per_replica,
            "request_latency_ms": self.request_latency_ms,
            "scale_threshold": self.scale_threshold,
            "min_replicas": self.min_replicas,
            "max_replicas": self.max_replicas,
            "concurrency_quota": self.concurrency_quota,
        }


@dataclass(frozen=True)
class ClusterDeclaration:
    cluster_name: str
    authored_version: int
    declaration_version: int
    global_replica_budget: int
    services: tuple[ServiceSpec, ...]
    raw_text: str
    fingerprint: str
    source_path: str
    document: Mapping[str, Any]
    schema_version: int = 1

    @property
    def total_minutes(self) -> int:
        return max((len(service.requests) for service in self.services), default=0)

    @property
    def service_names(self) -> tuple[str, ...]:
        return tuple(service.name for service in self.services)

    @property
    def service_map(self) -> dict[str, ServiceSpec]:
        return {service.name: service for service in self.services}

    def with_version(self, version: int) -> "ClusterDeclaration":
        return replace(self, declaration_version=int(version))

    def normalized_document(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "cluster_name": self.cluster_name,
            "declaration_version": self.declaration_version,
            "global_replica_budget": self.global_replica_budget,
            "services": [service.to_dict() for service in self.services],
        }


@dataclass(frozen=True)
class DemandEstimate:
    service: str
    minute: int
    request_count: int
    capacity_per_replica: int
    unclamped_demand: int
    demand_replicas: int
    min_replicas: int
    max_replicas: int
    scale_threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "minute": self.minute,
            "request_count": self.request_count,
            "capacity_per_replica": self.capacity_per_replica,
            "unclamped_demand": self.unclamped_demand,
            "demand_replicas": self.demand_replicas,
            "min_replicas": self.min_replicas,
            "max_replicas": self.max_replicas,
            "scale_threshold": self.scale_threshold,
        }


@dataclass(frozen=True)
class ClipReason:
    dimension: str
    before_replicas: int
    after_replicas: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimension": self.dimension,
            "before_replicas": self.before_replicas,
            "after_replicas": self.after_replicas,
        }


@dataclass(frozen=True)
class ServiceDecision:
    service: str
    minute: int
    request_count: int
    capacity_per_replica: int
    unclamped_demand: int
    demand_replicas: int
    concurrency_quota: int
    final_replicas: int
    capacity_requests: int
    accepted_requests: int
    rejected_requests: int
    clip_reasons: tuple[ClipReason, ...]
    request_latency_ms: int
    estimated_work_ms: int
    utilization: float
    threshold_triggered: bool
    min_replicas: int
    max_replicas: int
    scale_threshold: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "minute": self.minute,
            "request_count": self.request_count,
            "capacity_per_replica": self.capacity_per_replica,
            "unclamped_demand": self.unclamped_demand,
            "demand_replicas": self.demand_replicas,
            "concurrency_quota": self.concurrency_quota,
            "final_replicas": self.final_replicas,
            "capacity_requests": self.capacity_requests,
            "accepted_requests": self.accepted_requests,
            "rejected_requests": self.rejected_requests,
            "clip_reasons": [reason.to_dict() for reason in self.clip_reasons],
            "request_latency_ms": self.request_latency_ms,
            "estimated_work_ms": self.estimated_work_ms,
            "utilization": self.utilization,
            "threshold_triggered": self.threshold_triggered,
            "min_replicas": self.min_replicas,
            "max_replicas": self.max_replicas,
            "scale_threshold": self.scale_threshold,
        }


@dataclass(frozen=True)
class MinuteDecision:
    minute: int
    declaration_version: int
    budget_limit: int
    budget_requested_total: int
    budget_used: int
    requests_total: int
    accepted_total: int
    rejected_total: int
    estimated_work_ms_total: int
    services: tuple[ServiceDecision, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "minute": self.minute,
            "declaration_version": self.declaration_version,
            "budget_limit": self.budget_limit,
            "budget_requested_total": self.budget_requested_total,
            "budget_used": self.budget_used,
            "requests_total": self.requests_total,
            "accepted_total": self.accepted_total,
            "rejected_total": self.rejected_total,
            "estimated_work_ms_total": self.estimated_work_ms_total,
            "services": [service.to_dict() for service in self.services],
        }


@dataclass(frozen=True)
class VersionRegistration:
    declaration: ClusterDeclaration
    version_changed: bool
    previous_version: int | None
    differences: tuple[dict[str, Any], ...] = ()
    affected_services: tuple[str, ...] = ()
    affected_minute_range: tuple[int, int] | None = None
    stale_run_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "declaration_version": self.declaration.declaration_version,
            "authored_version": self.declaration.authored_version,
            "fingerprint": self.declaration.fingerprint,
            "version_changed": self.version_changed,
            "previous_version": self.previous_version,
            "differences": list(self.differences),
            "affected_services": list(self.affected_services),
            "affected_minute_range": (
                list(self.affected_minute_range)
                if self.affected_minute_range is not None
                else None
            ),
            "stale_run_ids": list(self.stale_run_ids),
        }


def sorted_dicts(values: Iterable[Mapping[str, Any]], key: str) -> list[dict[str, Any]]:
    return [dict(value) for value in sorted(values, key=lambda item: str(item[key]))]
