"""Data models shared by parsing, adjudication, and persistence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .codes import E_BAD_SCHEMA, SCHEMA_VERSION

IPv4Network = ipaddress.IPv4Network
IPv6Network = ipaddress.IPv6Network
IPNetwork = IPv4Network | IPv6Network

PROTOCOLS = frozenset({"tcp", "udp", "icmp", "sctp", "gre"})
PROTOCOL_NUMBERS = {"icmp": 1, "tcp": 6, "udp": 17, "gre": 47, "sctp": 132}
NUMBER_PROTOCOLS = {value: key for key, value in PROTOCOL_NUMBERS.items()}

ACTION_BY_CODE = {0: "deny", 1: "allow", 2: "log"}
ACTION_CODES = frozenset(ACTION_BY_CODE.values())
CONFLICT_POLICIES = frozenset({"strict", "deny_wins", "prefer_v2", "prefer_v1"})
MAPPING_DIRECTIONS = frozenset({"v1_to_v2", "v2_to_v1"})

SIDE_RANK = {"v1": 1, "v2": 2}


class AuditError(Exception):
    """A fatal input or persistence error with a stable business code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        source_category: str = "engine",
        record_index: int = 0,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.source_category = source_category
        self.record_index = record_index
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_category": self.source_category,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Rejection:
    source_category: str
    record_index: int
    code: str
    message: str
    rule_key: str | None = None
    case_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_category": self.source_category,
            "record_index": self.record_index,
            "code": self.code,
            "message": self.message,
            "rule_key": self.rule_key,
            "case_id": self.case_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Rejection":
        return cls(
            source_category=str(value["source_category"]),
            record_index=int(value["record_index"]),
            code=str(value["code"]),
            message=str(value.get("message", "")),
            rule_key=value.get("rule_key"),
            case_id=value.get("case_id"),
        )


@dataclass(frozen=True, slots=True)
class PortRange:
    lo: int
    hi: int

    def overlaps(self, other: "PortRange") -> bool:
        return self.lo <= other.hi and other.lo <= self.hi

    def to_list(self) -> list[int]:
        return [self.lo, self.hi]

    @classmethod
    def from_value(cls, value: Iterable[int]) -> "PortRange":
        lo, hi = value
        return cls(int(lo), int(hi))


@dataclass(frozen=True, slots=True)
class CaseTask:
    case_id: str
    sync_marker: str | None
    src_network: IPNetwork
    dst_network: IPNetwork
    protocol: str
    src_ports: PortRange
    dst_ports: PortRange
    declared_rule_seq: int | None = None
    declared_action: str | None = None
    record_index: int = 0

    def content_hash(self) -> str:
        return canonical_hash(self.to_dict(include_annotations=True))

    def to_dict(self, *, include_annotations: bool = True) -> dict[str, Any]:
        value = {
            "case_id": self.case_id,
            "sync_marker": self.sync_marker,
            "src_prefix": str(self.src_network),
            "dst_prefix": str(self.dst_network),
            "protocol": self.protocol,
            "src_port_range": f"{self.src_ports.lo}-{self.src_ports.hi}",
            "dst_port_range": f"{self.dst_ports.lo}-{self.dst_ports.hi}",
        }
        if include_annotations:
            value["declared_rule_seq"] = self.declared_rule_seq
            value["declared_action"] = self.declared_action
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CaseTask":
        return cls(
            case_id=str(value["case_id"]),
            sync_marker=value.get("sync_marker"),
            src_network=ipaddress.ip_network(value["src_prefix"], strict=False),
            dst_network=ipaddress.ip_network(value["dst_prefix"], strict=False),
            protocol=str(value["protocol"]),
            src_ports=PortRange.from_value(
                int(part) for part in str(value["src_port_range"]).split("-", 1)
            ),
            dst_ports=PortRange.from_value(
                int(part) for part in str(value["dst_port_range"]).split("-", 1)
            ),
            declared_rule_seq=value.get("declared_rule_seq"),
            declared_action=value.get("declared_action"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedRule:
    side: str
    rule_key: str
    src_network: IPNetwork
    dst_network: IPNetwork
    protocol: str
    src_ports: PortRange
    dst_ports: PortRange
    rule_seq: int
    action: str
    generation: int
    effective_ms: int
    tombstone: bool = False
    record_id: str = ""

    def matches(self, case: CaseTask) -> bool:
        return (
            self.protocol == case.protocol
            and network_contains(self.src_network, case.src_network)
            and network_contains(self.dst_network, case.dst_network)
            and self.src_ports.overlaps(case.src_ports)
            and self.dst_ports.overlaps(case.dst_ports)
        )

    def semantic_key(self) -> tuple[Any, ...]:
        return (
            str(self.src_network),
            str(self.dst_network),
            self.protocol,
            self.src_ports.lo,
            self.src_ports.hi,
            self.dst_ports.lo,
            self.dst_ports.hi,
            self.rule_seq,
            self.action,
        )

    def authority_key(self) -> tuple[Any, ...]:
        return (
            self.generation,
            self.effective_ms,
            1 if self.tombstone else 0,
            SIDE_RANK.get(self.side, 0),
            self.record_id,
        )

    def content_hash(self) -> str:
        return canonical_hash(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "rule_key": self.rule_key,
            "src_prefix": str(self.src_network),
            "dst_prefix": str(self.dst_network),
            "protocol": self.protocol,
            "src_ports": self.src_ports.to_list(),
            "dst_ports": self.dst_ports.to_list(),
            "rule_seq": self.rule_seq,
            "action": self.action,
            "generation": self.generation,
            "effective_ms": self.effective_ms,
            "tombstone": self.tombstone,
            "record_id": self.record_id,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NormalizedRule":
        return cls(
            side=str(value["side"]),
            rule_key=str(value["rule_key"]),
            src_network=ipaddress.ip_network(value["src_prefix"], strict=False),
            dst_network=ipaddress.ip_network(value["dst_prefix"], strict=False),
            protocol=str(value["protocol"]),
            src_ports=PortRange.from_value(value["src_ports"]),
            dst_ports=PortRange.from_value(value["dst_ports"]),
            rule_seq=int(value["rule_seq"]),
            action=str(value["action"]),
            generation=int(value["generation"]),
            effective_ms=int(value["effective_ms"]),
            tombstone=bool(value["tombstone"]),
            record_id=str(value.get("record_id", "")),
        )


@dataclass(frozen=True, slots=True)
class MappingEntry:
    v1_rule_key: str
    v2_rule_key: str
    mapping_generation: int
    record_index: int
    active: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "v1_rule_key": self.v1_rule_key,
            "v2_rule_key": self.v2_rule_key,
            "mapping_generation": self.mapping_generation,
            "record_index": self.record_index,
            "active": self.active,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MappingEntry":
        return cls(
            v1_rule_key=str(value["v1_rule_key"]),
            v2_rule_key=str(value["v2_rule_key"]),
            mapping_generation=int(value["mapping_generation"]),
            record_index=int(value.get("record_index", 0)),
            active=bool(value.get("active", True)),
        )


@dataclass(frozen=True, slots=True)
class MappingDeclaration:
    declaration_generation: int
    direction: str
    conflict_policy: str
    entries: tuple[MappingEntry, ...]
    rejections: tuple[Rejection, ...]
    path: str = ""


@dataclass(frozen=True, slots=True)
class MappingIssue:
    code: str
    rule_keys: tuple[str, ...]
    message: str
    pairs: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rule_keys": list(self.rule_keys),
            "message": self.message,
            "pairs": [list(pair) for pair in self.pairs],
        }


@dataclass(frozen=True, slots=True)
class MappingResult:
    declaration_generation: int
    conflict_policy: str
    direction: str
    entries: tuple[MappingEntry, ...]
    selected_entries: tuple[MappingEntry, ...]
    pairs: tuple[tuple[str, str], ...]
    pair_set: frozenset[tuple[str, str]]
    v1_to_v2: dict[str, tuple[str, ...]]
    v2_to_v1: dict[str, tuple[str, ...]]
    issues: tuple[MappingIssue, ...]
    cycle_rule_keys: tuple[str, ...]
    unknown_v1_keys: tuple[str, ...]
    unknown_v2_keys: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.issues

    def are_paired(self, v1_key: str, v2_key: str) -> bool:
        return (v1_key, v2_key) in self.pair_set

    def to_dict(self) -> dict[str, Any]:
        return {
            "declaration_generation": self.declaration_generation,
            "conflict_policy": self.conflict_policy,
            "direction": self.direction,
            "ok": self.ok,
            "pairs": [list(pair) for pair in self.pairs],
            "issues": [issue.to_dict() for issue in self.issues],
            "cycle_rule_keys": list(self.cycle_rule_keys),
            "unknown_v1_keys": list(self.unknown_v1_keys),
            "unknown_v2_keys": list(self.unknown_v2_keys),
        }


@dataclass(frozen=True, slots=True)
class SnapshotBundle:
    sample_id: str
    observed_clock_ms: int
    v1_generation: int
    v2_generation: int
    mapping_generation: int
    conflict_policy: str
    mapping_direction: str
    rules: tuple[NormalizedRule, ...]
    cases: tuple[CaseTask, ...]
    mapping_entries: tuple[MappingEntry, ...]
    rejections: tuple[Rejection, ...]
    snapshot_id: str = ""
    source: dict[str, str] = field(default_factory=dict)

    @property
    def rule_generation(self) -> int:
        return max(self.v1_generation, self.v2_generation)

    def rules_for_side(self, side: str) -> tuple[NormalizedRule, ...]:
        return tuple(rule for rule in self.rules if rule.side == side)

    def rules_by_key(self) -> dict[tuple[str, str], NormalizedRule]:
        return {(rule.side, rule.rule_key): rule for rule in self.rules}

    def case_map(self) -> dict[str, CaseTask]:
        return {case.case_id: case for case in self.cases}

    def rule_set_fingerprint(self) -> str:
        records = sorted(
            (
                {
                    "side": rule.side,
                    "rule_key": rule.rule_key,
                    "src_prefix": str(rule.src_network),
                    "dst_prefix": str(rule.dst_network),
                    "protocol": rule.protocol,
                    "src_ports": rule.src_ports.to_list(),
                    "dst_ports": rule.dst_ports.to_list(),
                    "rule_seq": rule.rule_seq,
                    "action": rule.action,
                    "generation": rule.generation,
                    "effective_ms": rule.effective_ms,
                    "tombstone": rule.tombstone,
                }
                for rule in self.rules
            ),
            key=lambda value: (
                value["side"],
                value["rule_key"],
                value["generation"],
                value["effective_ms"],
                value["tombstone"],
                canonical_json(value),
            ),
        )
        return canonical_hash(
            {
                "v1_generation": self.v1_generation,
                "v2_generation": self.v2_generation,
                "observed_clock_ms": self.observed_clock_ms,
                "rules": records,
            }
        )

    def mapping_fingerprint(self) -> str:
        unique: dict[tuple[str, str, int, bool], dict[str, Any]] = {}
        for entry in self.mapping_entries:
            value = {
                "v1_rule_key": entry.v1_rule_key,
                "v2_rule_key": entry.v2_rule_key,
                "mapping_generation": entry.mapping_generation,
                "active": entry.active,
            }
            unique[
                (
                    entry.v1_rule_key,
                    entry.v2_rule_key,
                    entry.mapping_generation,
                    entry.active,
                )
            ] = value
        entries = sorted(
            unique.values(),
            key=lambda value: (
                value["v1_rule_key"],
                value["v2_rule_key"],
                value["mapping_generation"],
                value["active"],
            ),
        )
        return canonical_hash(
            {
                "mapping_generation": self.mapping_generation,
                "direction": self.mapping_direction,
                "conflict_policy": self.conflict_policy,
                "entries": entries,
            }
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "sample_id": self.sample_id,
            "snapshot_id": self.snapshot_id,
            "observed_clock_ms": self.observed_clock_ms,
            "v1_generation": self.v1_generation,
            "v2_generation": self.v2_generation,
            "rule_generation": self.rule_generation,
            "mapping_generation": self.mapping_generation,
            "conflict_policy": self.conflict_policy,
            "mapping_direction": self.mapping_direction,
            "rules": [rule.to_dict() for rule in self.rules],
            "cases": [case.to_dict() for case in self.cases],
            "mapping_entries": [
                entry.to_dict() for entry in self.mapping_entries
            ],
            "rejections": [rejection.to_dict() for rejection in self.rejections],
            "source": dict(sorted(self.source.items())),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SnapshotBundle":
        if int(value.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise AuditError(
                E_BAD_SCHEMA,
                "unsupported snapshot schema version",
                detail={"schema_version": value.get("schema_version")},
            )
        return cls(
            sample_id=str(value["sample_id"]),
            observed_clock_ms=int(value["observed_clock_ms"]),
            v1_generation=int(value["v1_generation"]),
            v2_generation=int(value["v2_generation"]),
            mapping_generation=int(value["mapping_generation"]),
            conflict_policy=str(value["conflict_policy"]),
            mapping_direction=str(value["mapping_direction"]),
            rules=tuple(NormalizedRule.from_dict(item) for item in value["rules"]),
            cases=tuple(CaseTask.from_dict(item) for item in value["cases"]),
            mapping_entries=tuple(
                MappingEntry.from_dict(item) for item in value["mapping_entries"]
            ),
            rejections=tuple(
                Rejection.from_dict(item) for item in value.get("rejections", [])
            ),
            snapshot_id=str(value.get("snapshot_id", "")),
            source={
                str(key): str(item)
                for key, item in value.get("source", {}).items()
            },
        )


@dataclass(frozen=True, slots=True)
class SideAdjudication:
    status: str
    code: str | None
    rule_key: str | None
    action: str | None
    basis_generation: int | None
    rule_seq: int | None
    hits: tuple[dict[str, Any], ...] = ()
    effective_ms: int | None = None
    resolved_by_mapping: bool = False

    @property
    def hit_rule_keys(self) -> tuple[str, ...]:
        return tuple(hit["rule_key"] for hit in self.hits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "code": self.code,
            "rule_key": self.rule_key,
            "action": self.action,
            "basis_generation": self.basis_generation,
            "rule_seq": self.rule_seq,
            "effective_ms": self.effective_ms,
            "hits": list(self.hits),
            "resolved_by_mapping": self.resolved_by_mapping,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SideAdjudication":
        return cls(
            status=str(value["status"]),
            code=value.get("code"),
            rule_key=value.get("rule_key"),
            action=value.get("action"),
            basis_generation=value.get("basis_generation"),
            rule_seq=value.get("rule_seq"),
            hits=tuple(value.get("hits", [])),
            effective_ms=value.get("effective_ms"),
            resolved_by_mapping=bool(value.get("resolved_by_mapping", False)),
        )


@dataclass(frozen=True, slots=True)
class CaseConclusion:
    case_id: str
    sync_marker: str | None
    equivalent: bool
    code: str | None
    reason: str
    v1: SideAdjudication
    v2: SideAdjudication
    unified: SideAdjudication
    action_diff: dict[str, Any] | None = None
    case_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "sync_marker": self.sync_marker,
            "equivalent": self.equivalent,
            "code": self.code,
            "reason": self.reason,
            "v1": self.v1.to_dict(),
            "v2": self.v2.to_dict(),
            "unified": self.unified.to_dict(),
            "action_diff": self.action_diff,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CaseConclusion":
        return cls(
            case_id=str(value["case_id"]),
            sync_marker=value.get("sync_marker"),
            equivalent=bool(value["equivalent"]),
            code=value.get("code"),
            reason=str(value["reason"]),
            v1=SideAdjudication.from_dict(value["v1"]),
            v2=SideAdjudication.from_dict(value["v2"]),
            unified=SideAdjudication.from_dict(value["unified"]),
            action_diff=value.get("action_diff"),
            case_hash=str(value.get("case_hash", "")),
        )


@dataclass(frozen=True, slots=True)
class RuleView:
    side: str
    active: dict[str, NormalizedRule]
    shadow: dict[str, NormalizedRule]

    def all_active(self) -> tuple[NormalizedRule, ...]:
        return tuple(self.active[key] for key in sorted(self.active))

    def all_shadow(self) -> tuple[NormalizedRule, ...]:
        return tuple(self.shadow[key] for key in sorted(self.shadow))


def network_contains(container: IPNetwork, contained: IPNetwork) -> bool:
    if container.version != contained.version:
        return False
    return contained.subnet_of(container)  # type: ignore[arg-type]


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
