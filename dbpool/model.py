from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Endpoint:
    name: str
    address: str
    host: str
    port: int
    max_connections: int
    build_latency_ms: int
    healthy: bool
    pool_labels: tuple[str, ...]

    def canonical(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "address": self.address,
            "max_connections": self.max_connections,
            "build_latency_ms": self.build_latency_ms,
            "healthy": self.healthy,
            "pool_labels": list(self.pool_labels),
        }


@dataclass(frozen=True)
class PoolConfig:
    version: int
    max_total_connections: int
    per_tenant_quota: int
    idle_timeout_ms: int
    min_idle: int
    round_budget_ms: int
    clock_step_ms: int
    bucket_width_ms: int
    switch_margin_ratio: float
    tiered_min_idle: int
    idle_age_tolerance_ms: int
    mode: str = "auto"
    tenant_pool_labels: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "max_total_connections": self.max_total_connections,
            "per_tenant_quota": self.per_tenant_quota,
            "idle_timeout_ms": self.idle_timeout_ms,
            "min_idle": self.min_idle,
            "round_budget_ms": self.round_budget_ms,
            "clock_step_ms": self.clock_step_ms,
            "bucket_width_ms": self.bucket_width_ms,
            "switch_margin_ratio": self.switch_margin_ratio,
            "tiered_min_idle": self.tiered_min_idle,
            "idle_age_tolerance_ms": self.idle_age_tolerance_ms,
            "mode": self.mode,
            "tenant_pool_labels": {
                tenant: list(labels)
                for tenant, labels in sorted(self.tenant_pool_labels.items())
            },
        }


@dataclass(frozen=True)
class Application:
    seq: int
    version: int
    tenant: str
    pool_label: str
    apply_clock_ms: int
    hold_ms: int
    release: bool

    def canonical(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "version": self.version,
            "tenant": self.tenant,
            "pool_label": self.pool_label,
            "apply_clock_ms": self.apply_clock_ms,
            "hold_ms": self.hold_ms,
            "release": self.release,
        }


@dataclass(frozen=True)
class Trace:
    version: int
    applications: tuple[Application, ...]
    final_clock_ms: int | None = None

    def canonical(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "final_clock_ms": self.final_clock_ms,
            "applications": [
                application.canonical()
                for application in sorted(
                    self.applications,
                    key=lambda item: (item.seq, item.version),
                )
            ],
        }


@dataclass(frozen=True)
class Catalog:
    config: PoolConfig
    endpoints: tuple[Endpoint, ...]
    trace: Trace
    tenant_pool_labels: dict[str, tuple[str, ...]]
    endpoint_digest: str = ""
    config_digest: str = ""
    trace_digest: str = ""
    sample_digest: str = ""

    def canonical(self) -> dict[str, Any]:
        return {
            "config": self.config.canonical(),
            "endpoints": [endpoint.canonical() for endpoint in self.endpoints],
            "trace": self.trace.canonical(),
            "tenant_pool_labels": {
                tenant: list(labels)
                for tenant, labels in sorted(self.tenant_pool_labels.items())
            },
        }
