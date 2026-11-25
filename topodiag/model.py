"""Validated domain objects shared by all execution paths."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ServiceTemplate:
    name: str
    version: int
    dependencies: tuple[str, ...]
    labels: tuple[str, ...]
    logical_clock: int
    resources: tuple[tuple[str, int], ...]

    def resources_dict(self) -> dict[str, int]:
        return dict(self.resources)


@dataclass(frozen=True)
class InstantiationRequest:
    request_seq: int
    template_name: str
    target_topology_namespace: str
    instance_id: str
    desired_state: str
    logical_clock: int
    round_no: int
    logical_cost_ms: int
    inject_start_failure: bool = False


@dataclass(frozen=True)
class NamespaceTopology:
    name: str
    node_id: str
    max_depth: int


@dataclass(frozen=True)
class Topology:
    nodes: dict[str, dict[str, Any]]
    namespaces: dict[str, NamespaceTopology]


@dataclass
class PolicyCandidate:
    """A merged policy entry before persistence."""

    template_name: str
    version: int
    dependencies: tuple[str, ...]
    labels: tuple[str, ...]
    logical_clock: int
    policy_generation: int
    source_format: str
    source_version: str
    original_time: dict[str, Any]
    conversion: dict[str, Any]
    effective_at_ms: int
    decision_basis: str
    decision_detail: str
    tombstone: bool = False
    tombstone_reason: str | None = None

    def node_payload(self) -> dict[str, Any]:
        return {
            "template_name": self.template_name,
            "version": self.version,
            "dependencies": list(self.dependencies),
            "labels": list(self.labels),
            "logical_clock": self.logical_clock,
            "policy_generation": self.policy_generation,
            "source_format": self.source_format,
            "source_version": self.source_version,
            "original_time": self.original_time,
            "conversion": self.conversion,
            "effective_at_ms": self.effective_at_ms,
            "decision_basis": self.decision_basis,
            "decision_detail": self.decision_detail,
            "tombstone": self.tombstone,
            "tombstone_reason": self.tombstone_reason,
        }


@dataclass
class V1Policy:
    policy_key: str
    policy_generation: int
    manifest_version: str
    logical_clock: int
    relative_effective_seconds: int
    no_topology_constraints: bool
    entries: dict[str, dict[str, Any]]


@dataclass
class V2Tombstone:
    template_name: str
    version: int
    logical_clock: int
    reason: str


@dataclass
class TopologyInvalidation:
    scope: str
    logical_clock: int
    reason: str


@dataclass
class V2PolicyPackage:
    path: str
    policy_key: str
    policy_generation: int
    generation_floor: int
    manifest_version: str
    logical_clock: int
    absolute_effective_ms: int
    update_mode: str
    base_generation: int | None
    dependency_digest: str
    shard_count: int
    entries: dict[str, dict[str, Any]]
    tombstones: list[V2Tombstone]
    topology_invalidations: list[TopologyInvalidation]
    bundle_digest: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClosureRecord:
    template_name: str
    closure: tuple[str, ...]
    closure_digest: str
    config_fingerprint: str
    dependency_digest: str
    revision: int
    policy_version_row: int = 0


@dataclass
class InvalidationEvent:
    trigger_template: str
    trigger_version: int
    trigger_generation: int
    trigger_clock: int
    reason: str
    changed_templates: tuple[str, ...]
    affected_templates: tuple[str, ...]
    chains: tuple[tuple[str, ...], ...]
    before: dict[str, Any]
    after: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "trigger": {
                "template_name": self.trigger_template,
                "version": self.trigger_version,
                "policy_generation": self.trigger_generation,
                "logical_clock": self.trigger_clock,
            },
            "reason": self.reason,
            "changed_templates": list(self.changed_templates),
            "affected_downstream": list(self.affected_templates),
            "chains": [list(chain) for chain in self.chains],
            "versions_before": self.before,
            "versions_after": self.after,
        }
