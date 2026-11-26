from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Config:
    tenant: str
    resource_key: str
    baseline_micros: int
    baseline_error_micros: int
    baseline_rounding: str
    baseline_input: str
    elasticity: Fraction
    elasticity_text: str
    lease_duration_ms: int
    quota_generation: int
    depends_on_resource_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class Application:
    application_seq: int
    tenant: str
    resource_key: str
    requested_micros: int
    requested_error_micros: int
    requested_rounding: str
    requested_input: str
    logical_clock_ms: int
    priority: int


@dataclass(frozen=True)
class Release:
    release_seq: int
    application_seq: int
    tenant: str
    resource_key: str
    actual_micros: int
    actual_error_micros: int
    actual_rounding: str
    actual_input: str
    logical_clock_ms: int


@dataclass(frozen=True)
class Observation:
    observation_seq: int
    tenant: str
    resource_key: str
    actual_occupancy_micros: int
    actual_error_micros: int
    actual_rounding: str
    actual_input: str
    logical_clock_ms: int


@dataclass(frozen=True)
class PolicyParams:
    global_quota_budget_micros: int
    round_threshold_budget_micros: int
    per_tenant_adjustment_cap_micros: int
    safety_watermark_micros: int
    drift_threshold_ratio: Fraction
    drift_threshold_text: str
    danger_threshold_ratio: Fraction
    danger_threshold_text: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "global_quota_budget_micros": self.global_quota_budget_micros,
            "round_threshold_budget_micros": self.round_threshold_budget_micros,
            "per_tenant_adjustment_cap_micros": self.per_tenant_adjustment_cap_micros,
            "safety_watermark_micros": self.safety_watermark_micros,
            "drift_threshold_ratio": self.drift_threshold_text,
            "danger_threshold_ratio": self.danger_threshold_text,
        }


@dataclass(frozen=True)
class Tombstone:
    target_snapshot_id: str
    target_version: str
    tenant: str
    resource_key: str
    invalidated_generation: int
    effective_clock_ms: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_snapshot_id": self.target_snapshot_id,
            "target_version": self.target_version,
            "tenant": self.tenant,
            "resource_key": self.resource_key,
            "invalidated_generation": self.invalidated_generation,
            "effective_clock_ms": self.effective_clock_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ResourceDependency:
    resource_key: str
    depends_on: str

    def as_dict(self) -> dict[str, Any]:
        return {"resource_key": self.resource_key, "depends_on": self.depends_on}


@dataclass(frozen=True)
class Snapshot:
    policy_id: str
    snapshot_id: str
    version: str
    generation: int
    created_clock_ms: int
    effective_clock_ms: int
    params: PolicyParams
    thresholds: dict[str, int]
    tombstones: tuple[Tombstone, ...]
    dependencies: tuple[ResourceDependency, ...]
    shards: tuple[dict[str, Any], ...]
    source_path: str
    fingerprint: str

    @property
    def version_rank(self) -> int:
        return 2 if self.version == "v2" else 1

    def as_canonical(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "snapshot_id": self.snapshot_id,
            "version": self.version,
            "generation": self.generation,
            "created_clock_ms": self.created_clock_ms,
            "effective_clock_ms": self.effective_clock_ms,
            "params": self.params.as_dict(),
            "thresholds": self.thresholds,
            "tombstones": [item.as_dict() for item in self.tombstones],
            "dependencies": [item.as_dict() for item in self.dependencies],
            "shards": list(self.shards),
        }


@dataclass
class Dataset:
    data_dir: Path
    configs: tuple[Config, ...]
    applications: tuple[Application, ...]
    releases: tuple[Release, ...]
    observations: tuple[Observation, ...]
    snapshots: tuple[Snapshot, ...]
    round_span_ms: int
    digest: str
    config_by_tenant: dict[str, Config] = field(init=False)

    def __post_init__(self) -> None:
        self.config_by_tenant = {item.tenant: item for item in self.configs}

    @property
    def resource_keys(self) -> frozenset[str]:
        return frozenset(item.resource_key for item in self.configs)

    @property
    def target_round(self) -> int:
        event_clocks = [item.logical_clock_ms for item in self.applications]
        event_clocks.extend(item.logical_clock_ms for item in self.releases)
        event_clocks.extend(item.logical_clock_ms for item in self.observations)
        if self.snapshots:
            event_clocks.extend(item.effective_clock_ms for item in self.snapshots)
        maximum = max(event_clocks, default=0)
        return maximum // self.round_span_ms + 1

    def as_canonical(self) -> dict[str, Any]:
        return {
            "round_span_ms": self.round_span_ms,
            "configs": [
                {
                    "tenant": item.tenant,
                    "resource_key": item.resource_key,
                    "baseline_micros": item.baseline_micros,
                    "elasticity": item.elasticity_text,
                    "lease_duration_ms": item.lease_duration_ms,
                    "quota_generation": item.quota_generation,
                    "depends_on_resource_keys": list(item.depends_on_resource_keys),
                }
                for item in self.configs
            ],
            "applications": [
                {
                    "application_seq": item.application_seq,
                    "tenant": item.tenant,
                    "resource_key": item.resource_key,
                    "requested_micros": item.requested_micros,
                    "logical_clock_ms": item.logical_clock_ms,
                    "priority": item.priority,
                }
                for item in self.applications
            ],
            "releases": [
                {
                    "release_seq": item.release_seq,
                    "application_seq": item.application_seq,
                    "tenant": item.tenant,
                    "resource_key": item.resource_key,
                    "actual_micros": item.actual_micros,
                    "logical_clock_ms": item.logical_clock_ms,
                }
                for item in self.releases
            ],
            "observations": [
                {
                    "observation_seq": item.observation_seq,
                    "tenant": item.tenant,
                    "resource_key": item.resource_key,
                    "actual_micros": item.actual_occupancy_micros,
                    "logical_clock_ms": item.logical_clock_ms,
                }
                for item in self.observations
            ],
            "snapshots": [item.as_canonical() for item in self.snapshots],
        }

