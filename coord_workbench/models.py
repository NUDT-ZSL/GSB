"""Typed domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class Node:
    name: str
    role: str
    candidate_addr: str
    lease_epoch: int
    priority: int
    health: str


@dataclass(frozen=True)
class Event:
    seq: int
    node: str
    clock_ms: int
    candidate_term: int
    vote: Dict[str, Any]
    revoked: bool


@dataclass(frozen=True)
class Subscription:
    service: str
    subscriber: str
    callback: str


@dataclass(frozen=True)
class TopoEntry:
    """A topology entry normalized from either snapshot format."""

    node: str
    scope: str
    depends_on: List[str]
    source_version: str  # "v1" | "v2"
    raw_time: Dict[str, Any]  # original time representation
    abs_ms: int  # absolute publish time on the injected logical clock
    tombstone: bool = False


@dataclass
class MergedTopoView:
    """Per-node merged topology view (req 2)."""

    node: str
    scope: str
    depends_on: List[str]
    source_version: str
    raw_time: Dict[str, Any]
    abs_ms: int
    decided_by: str
    conversion: Optional[Dict[str, Any]] = None  # v1 rel->abs conversion record


@dataclass
class Batch:
    """A topology batch of committed events (req 3)."""

    batch_id: str
    domain: str
    topo_epoch: int
    events: List[Event] = field(default_factory=list)
    est_cost_ms: int = 0


# Batch outcomes under the scheduler (req 4)
OUTCOME_COMMITTED = "COMMITTED"
OUTCOME_NOT_STARTED = "NOT_STARTED_BUDGET"
OUTCOME_START_FAILED = "START_FAILED"
OUTCOME_ABANDONED = "ABANDONED"
