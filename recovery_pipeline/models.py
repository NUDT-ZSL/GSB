"""Data model shared across loaders, merge, migration and execution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(obj) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class V1Action:
    action_id: str
    at_tick: int
    op: str  # "start" | "stop" | "switch"


@dataclass(frozen=True)
class V1Service:
    name: str
    fault_domain: str
    rollback_seconds: int
    actions: tuple  # tuple[V1Action, ...] in declared order


@dataclass(frozen=True)
class V1Config:
    services: tuple  # tuple[V1Service, ...]
    action_map: dict  # service name -> [action_id, ...]


@dataclass(frozen=True)
class V2Service:
    name: str
    effective_ms: int
    generation: int
    priority: str  # e.g. "P0".."P3"
    upstream: tuple  # tuple[str, ...]
    tombstone: bool


@dataclass(frozen=True)
class V2Config:
    manifest_version: int
    generation_floor: int
    services: tuple  # tuple[V2Service, ...] (tombstoned included pre-filter)
    shard_digests: dict  # shard path -> sha256 hex


@dataclass(frozen=True)
class UnifiedAction:
    service: str
    action_id: str
    op: str
    source_version: str  # "v1" | "v2"
    original_time_repr: str  # "tick:5" or "ms:1700000000000"
    original_rollback_seconds: int | None  # preserved from v1, None for v2
    effective_ms: int  # unified absolute logical time
    generation: int
    priority: str
    ordinal: int  # original declaration order, for stable sorting


@dataclass
class ServicePlan:
    service: str
    fault_domain: str
    upstream: tuple = ()
    generation: int = 1
    priority: str = "P3"
    rollback_abs_ms: int | None = None
    traffic_ladder: tuple = (10, 25, 50, 100)
    actions: list = field(default_factory=list)  # list[UnifiedAction]

    def view(self) -> dict:
        return {
            "service": self.service,
            "fault_domain": self.fault_domain,
            "upstream": list(self.upstream),
            "generation": self.generation,
            "priority": self.priority,
            "rollback_abs_ms": self.rollback_abs_ms,
            "traffic_ladder": list(self.traffic_ladder),
            "actions": [asdict(a) for a in self.actions],
        }


@dataclass
class UnifiedPlan:
    services: dict = field(default_factory=dict)  # name -> ServicePlan
    migration_map: dict = field(default_factory=dict)  # service -> mapping record

    def view(self) -> dict:
        return {
            "services": {k: self.services[k].view() for k in sorted(self.services)},
            "migration_map": self.migration_map,
        }

    def digest(self) -> str:
        return digest_of(self.view())
