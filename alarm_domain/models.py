"""Immutable domain value objects and their canonical projections."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .util import canonical_json


SEVERITIES = ("INFO", "WARNING", "ERROR", "CRITICAL")
SEVERITY_RANK = {name: index for index, name in enumerate(SEVERITIES, start=1)}
SUPPORTED_OPERATORS = (">", ">=", "<", "<=", "==", "!=")


@dataclass(frozen=True)
class Rule:
    rule_id: str
    service: str
    severity: str
    condition: dict[str, Any]

    @property
    def severity_rank(self) -> int:
        return SEVERITY_RANK[self.severity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "service": self.service,
            "severity": self.severity,
            "condition": {
                "metric": self.condition["metric"],
                "operator": self.condition["operator"],
                "threshold": self.condition["threshold"],
            },
        }

    @property
    def canonical(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True, order=True)
class Edge:
    upstream: str
    downstream: str
    weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "upstream": self.upstream,
            "downstream": self.downstream,
            "weight": self.weight,
        }


@dataclass(frozen=True)
class Topology:
    services: tuple[str, ...]
    edges: tuple[Edge, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": list(self.services),
            "edges": [edge.to_dict() for edge in self.edges],
        }

    @property
    def canonical(self) -> str:
        return canonical_json(self.to_dict())


@dataclass(frozen=True)
class AlarmEvent:
    event_id: str
    rule_id: str
    service: str
    timestamp: int
    value: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "rule_id": self.rule_id,
            "service": self.service,
            "timestamp": self.timestamp,
            "value": self.value,
        }


@dataclass(frozen=True)
class Cluster:
    cluster_id: str
    members: tuple[str, ...]
    services: tuple[str, ...]
    start_ts: int
    end_ts: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "members": list(self.members),
            "services": list(self.services),
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
        }


def rule_index(rules: tuple[Rule, ...] | list[Rule]) -> dict[str, Rule]:
    return {rule.rule_id: rule for rule in rules}
