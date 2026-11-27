from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

REFERENCE_ERROR = "E_REFERENCE"

READY = "ready"
LOAD_ERROR = "load_error"
ALL_FILTERED = "all_filtered"
SINGLE_CANDIDATE = "single_candidate"


def _as_tuple(values: Iterable[Any] | None) -> tuple[Any, ...]:
    return tuple(values or ())


@dataclass(frozen=True)
class Region:
    id: str
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class BuildNode:
    id: str
    name: str
    region_id: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "region_id": self.region_id}


@dataclass(frozen=True)
class NextHop:
    id: str
    name: str
    region_id: str
    asn: int
    access_point: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "region_id": self.region_id,
            "asn": self.asn,
            "access_point": self.access_point,
        }


@dataclass(frozen=True)
class RouteCandidate:
    id: str
    next_hop_id: str
    as_path: tuple[int, ...]
    local_pref: int
    communities: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "next_hop_id": self.next_hop_id,
            "as_path": list(self.as_path),
            "local_pref": self.local_pref,
            "communities": list(self.communities),
        }


@dataclass(frozen=True)
class Prefix:
    id: str
    cidr: str
    build_node_id: str
    candidates: tuple[RouteCandidate, ...]
    repository: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "cidr": self.cidr,
            "build_node_id": self.build_node_id,
            "repository": self.repository,
            "candidates": [candidate.to_dict() for candidate in self.candidates],
        }


@dataclass(frozen=True)
class FilterRule:
    id: str
    match: str
    communities: tuple[str, ...]
    action: str = "deny"

    def matched_communities(self, candidate_communities: Sequence[str]) -> tuple[str, ...]:
        candidate_set = set(candidate_communities)
        if self.match == "all":
            return tuple(self.communities) if set(self.communities) <= candidate_set else ()
        return tuple(community for community in self.communities if community in candidate_set)

    def excludes(self, candidate_communities: Sequence[str]) -> bool:
        matched = self.matched_communities(candidate_communities)
        if self.action == "deny":
            return bool(matched)
        if self.action == "allow":
            return not bool(matched)
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "match": self.match,
            "communities": list(self.communities),
            "action": self.action,
        }


@dataclass(frozen=True)
class Policy:
    community_weights: Mapping[str, int] = field(default_factory=dict)
    filters: tuple[FilterRule, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "community_weights": {
                key: self.community_weights[key]
                for key in sorted(self.community_weights)
            },
            "filters": [rule.to_dict() for rule in self.filters],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Policy":
        return Policy(
            community_weights={
                str(key): int(value)
                for key, value in (raw.get("community_weights") or {}).items()
            },
            filters=tuple(
                FilterRule(
                    id=str(item["id"]),
                    match=str(item.get("match", "any")),
                    communities=tuple(
                        sorted(
                            str(value)
                            for value in item.get("communities", ())
                        )
                    ),
                    action=str(item.get("action", "deny")),
                )
                for item in raw.get("filters", ())
            ),
        )


@dataclass(frozen=True)
class ReferenceIssue:
    code: str
    entity: str
    entity_id: str
    field: str
    reference: str
    message: str
    source_schema: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "entity": self.entity,
            "entity_id": self.entity_id,
            "field": self.field,
            "reference": self.reference,
            "message": self.message,
            "source_schema": self.source_schema,
        }


@dataclass(frozen=True)
class NormalizedProject:
    project_id: str
    name: str
    schema_version: int
    source_schema: str
    seed: int
    regions: tuple[Region, ...]
    build_nodes: tuple[BuildNode, ...]
    next_hops: tuple[NextHop, ...]
    prefixes: tuple[Prefix, ...]
    policy: Policy
    local_pref_scale: Mapping[str, Any]
    reference_issues: tuple[ReferenceIssue, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def region_by_id(self) -> dict[str, Region]:
        return {item.id: item for item in self.regions}

    def node_by_id(self) -> dict[str, BuildNode]:
        return {item.id: item for item in self.build_nodes}

    def next_hop_by_id(self) -> dict[str, NextHop]:
        return {item.id: item for item in self.next_hops}

    def prefix_by_id(self) -> dict[str, Prefix]:
        return {item.id: item for item in self.prefixes}

    def prefix_issues(self) -> dict[str, list[ReferenceIssue]]:
        result: dict[str, list[ReferenceIssue]] = {}
        for issue in self.reference_issues:
            if issue.entity == "prefix" or issue.entity == "candidate":
                result.setdefault(issue.entity_id.split(":", 1)[0], []).append(issue)
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "project": {
                "id": self.project_id,
                "name": self.name,
                "seed": self.seed,
            },
            "schema_version": self.schema_version,
            "source_schema": self.source_schema,
            "local_pref_scale": dict(self.local_pref_scale),
            "policy": self.policy.to_dict(),
            "regions": [item.to_dict() for item in self.regions],
            "build_nodes": [item.to_dict() for item in self.build_nodes],
            "next_hops": [item.to_dict() for item in self.next_hops],
            "prefixes": [item.to_dict() for item in self.prefixes],
            "reference_issues": [item.to_dict() for item in self.reference_issues],
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class PathDecision:
    candidate_id: str
    next_hop_id: str
    as_path: tuple[int, ...]
    local_pref: int
    communities: tuple[str, ...]
    community_weight: int
    rank: int
    why: str

    @property
    def as_path_length(self) -> int:
        return len(self.as_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "next_hop_id": self.next_hop_id,
            "as_path": list(self.as_path),
            "as_path_length": self.as_path_length,
            "local_pref": self.local_pref,
            "communities": list(self.communities),
            "community_weight": self.community_weight,
            "rank": self.rank,
            "why": self.why,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "PathDecision | None":
        if not raw:
            return None
        return cls(
            candidate_id=str(raw["candidate_id"]),
            next_hop_id=str(raw["next_hop_id"]),
            as_path=tuple(int(value) for value in raw.get("as_path", ())),
            local_pref=int(raw["local_pref"]),
            communities=tuple(str(value) for value in raw.get("communities", ())),
            community_weight=int(raw.get("community_weight", 0)),
            rank=int(raw.get("rank", 0)),
            why=str(raw.get("why", "")),
        )


@dataclass(frozen=True)
class CandidateAssessment:
    candidate_id: str
    next_hop_id: str
    as_path: tuple[int, ...]
    local_pref: int
    communities: tuple[str, ...]
    community_weight: int
    eligible: bool
    excluded_by: tuple[str, ...]
    exclusion_reason: str
    rank: int | None = None
    why: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "next_hop_id": self.next_hop_id,
            "as_path": list(self.as_path),
            "as_path_length": len(self.as_path),
            "local_pref": self.local_pref,
            "communities": list(self.communities),
            "community_weight": self.community_weight,
            "eligible": self.eligible,
            "excluded_by": list(self.excluded_by),
            "exclusion_reason": self.exclusion_reason,
            "rank": self.rank,
            "why": self.why,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CandidateAssessment":
        return cls(
            candidate_id=str(raw["candidate_id"]),
            next_hop_id=str(raw["next_hop_id"]),
            as_path=tuple(int(value) for value in raw.get("as_path", ())),
            local_pref=int(raw["local_pref"]),
            communities=tuple(str(value) for value in raw.get("communities", ())),
            community_weight=int(raw.get("community_weight", 0)),
            eligible=bool(raw.get("eligible", False)),
            excluded_by=tuple(str(value) for value in raw.get("excluded_by", ())),
            exclusion_reason=str(raw.get("exclusion_reason", "")),
            rank=int(raw["rank"]) if raw.get("rank") is not None else None,
            why=str(raw.get("why", "")),
        )


@dataclass(frozen=True)
class SelectionResult:
    prefix_id: str
    node_id: str
    best: PathDecision | None
    backup: PathDecision | None
    assessments: tuple[CandidateAssessment, ...]
    available_count: int
    filtered_count: int
    down_count: int
    load_error_count: int
    status: str
    basis: Mapping[str, Any]

    def selected_fingerprint(self) -> tuple[Any, ...]:
        return (
            self.status,
            self.best.next_hop_id if self.best else None,
            tuple(self.best.as_path) if self.best else None,
            self.best.local_pref if self.best else None,
            self.best.community_weight if self.best else None,
            self.backup.next_hop_id if self.backup else None,
            tuple(self.backup.as_path) if self.backup else None,
            self.backup.local_pref if self.backup else None,
            self.backup.community_weight if self.backup else None,
        )

    def selection_fingerprint(self) -> tuple[Any, ...]:
        return (
            self.status,
            self.best.candidate_id if self.best else None,
            self.backup.candidate_id if self.backup else None,
            self.available_count,
            tuple(
                (
                    item.candidate_id,
                    item.eligible,
                    item.rank,
                    item.excluded_by,
                )
                for item in self.assessments
            ),
        )

    def selected_paths_changed(self, other: "SelectionResult") -> bool:
        return self.selected_fingerprint() != other.selected_fingerprint()

    def to_dict(self) -> dict[str, Any]:
        return {
            "prefix_id": self.prefix_id,
            "node_id": self.node_id,
            "best": self.best.to_dict() if self.best else None,
            "backup": self.backup.to_dict() if self.backup else None,
            "assessments": [item.to_dict() for item in self.assessments],
            "available_count": self.available_count,
            "filtered_count": self.filtered_count,
            "down_count": self.down_count,
            "load_error_count": self.load_error_count,
            "status": self.status,
            "basis": dict(self.basis),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SelectionResult":
        return cls(
            prefix_id=str(raw["prefix_id"]),
            node_id=str(raw["node_id"]),
            best=PathDecision.from_dict(raw.get("best")),
            backup=PathDecision.from_dict(raw.get("backup")),
            assessments=tuple(
                CandidateAssessment.from_dict(item) for item in raw.get("assessments", ())
            ),
            available_count=int(raw.get("available_count", 0)),
            filtered_count=int(raw.get("filtered_count", 0)),
            down_count=int(raw.get("down_count", 0)),
            load_error_count=int(raw.get("load_error_count", 0)),
            status=str(raw["status"]),
            basis=dict(raw.get("basis", {})),
        )
