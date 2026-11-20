from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Anchor:
    alias: str
    fingerprint: str
    cert_file: str
    not_before_ms: int
    not_after_ms: int
    revoked: bool
    can_issue_intermediates: bool
    trust_generation: int
    conversion: dict[str, Any] = field(default_factory=dict)

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "alias": self.alias,
            "fingerprint": self.fingerprint,
            "cert_file": self.cert_file,
            "not_before_ms": self.not_before_ms,
            "not_after_ms": self.not_after_ms,
            "revoked": self.revoked,
            "can_issue_intermediates": self.can_issue_intermediates,
        }


@dataclass(frozen=True)
class Snapshot:
    snapshot_version: str
    generation: int
    created_clock_ms: int
    source_digest: str
    kind: str
    anchors: tuple[Anchor, ...]

    def anchor_map(self) -> dict[str, Anchor]:
        return {anchor.alias: anchor for anchor in self.anchors}


@dataclass(frozen=True)
class ChainRequest:
    request_id: str
    hostname: str
    purpose: str
    logical_clock_ms: int
    require_revocation: bool
    leaf_file: str


@dataclass
class NodeEvidence:
    position: int
    subject: str
    issuer: str
    serial_hex: str
    fingerprint: str
    not_before_ms: int
    not_after_ms: int
    is_anchor: bool
    is_leaf: bool
    basic_constraints_ca: bool
    path_length: int | None
    key_usage: list[str]
    extended_key_usage: list[str]
    checks: dict[str, bool]
    failure_codes: list[str] = field(default_factory=list)


@dataclass
class ChainResult:
    request: ChainRequest
    status: str
    reason_code: str
    reason: str
    chain_fingerprints: list[str]
    nodes: list[NodeEvidence]
    anchor_alias: str | None
    anchor_fingerprint: str | None
    hostname_check: dict[str, Any]


@dataclass
class RevisionState:
    status: str
    evidence: dict[str, Any]

