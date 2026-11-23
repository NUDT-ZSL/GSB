"""Typed in memory models for services, nodes and scaling strategies."""

from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class Service:
    name: str
    replicas: int
    cpu_per_replica: int
    mem_per_replica: int
    upstream: tuple
    failure_domain: str
    priority: int

    def to_dict(self):
        row = asdict(self)
        row["upstream"] = list(self.upstream)
        return row


@dataclass(frozen=True)
class Node:
    name: str
    cpu_total: int
    mem_total: int
    cpu_free: int
    mem_free: int
    topology_domain: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class Gear:
    relative_seconds: int
    ratio: str


@dataclass(frozen=True)
class V1Policy:
    service: str
    gears: tuple


@dataclass(frozen=True)
class V2Gear:
    absolute_ms: int
    ratio: str


@dataclass(frozen=True)
class V2Shard:
    shard_id: str
    generation: int
    shard_hash: str
    declared_clock_ms: int
    services: tuple
    gears: tuple
    tombstones: tuple


@dataclass(frozen=True)
class UnifiedGear:
    """One gear in the merged view with full provenance."""

    service: str
    source_version: str
    generation: int
    logical_clock: int
    absolute_ms: int
    ratio: str
    raw_time: str
    decision_basis: str
    conversion_scale: int
    rounding: str

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class UnifiedPolicy:
    service: str
    source_version: str
    generation: int
    logical_clock: int
    decision_basis: str
    tombstoned: bool
    gears: tuple

    def to_dict(self):
        row = asdict(self)
        row["gears"] = [gear.to_dict() for gear in self.gears]
        return row


@dataclass
class ServiceConclusion:
    """Reusable per service scaling outcome for a round."""

    service: str
    round_no: int
    policy_version: str
    logical_clock: int
    current_replicas: int
    target_replicas: int
    action: str
    failure_domain: str
    priority: int
    sensitive: bool
    error_chain: list
    placements: list = field(default_factory=list)
    rejected_candidates: list = field(default_factory=list)
    first_constraint: str = ""
    result: str = ""
    reason: str = ""
    ledger_snapshot: dict = field(default_factory=dict)
    input_fingerprint: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, row):
        return cls(**row)
