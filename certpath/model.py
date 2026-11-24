"""Domain dataclasses used by parsing, merging, and adjudication."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Certificate:
    certificate_id: str
    subject_id: str
    issuer_id: str
    public_key_fingerprint: str
    signature_digest: str
    not_before_ms: int
    not_after_ms: int
    cross_signed: bool
    revoked: bool
    record_index: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "certificate_id": self.certificate_id,
            "subject_id": self.subject_id,
            "issuer_id": self.issuer_id,
            "public_key_fingerprint": self.public_key_fingerprint,
            "signature_digest": self.signature_digest,
            "not_before_ms": self.not_before_ms,
            "not_after_ms": self.not_after_ms,
            "cross_signed": self.cross_signed,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class ChainDeclaration:
    chain_id: str
    certificate_ids: tuple[str, ...]
    enumeration_limit: int
    record_index: int

    @property
    def leaf_id(self) -> str:
        return self.certificate_ids[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "certificate_ids": list(self.certificate_ids),
            "enumeration_limit": self.enumeration_limit,
        }


@dataclass(frozen=True)
class V1AnchorRecord:
    certificate_id: str
    fingerprint: str
    subject_id: str
    relative_effective_seconds: int
    effective_ms: int
    effective_low_ms: int
    effective_high_ms: int
    trusted: bool
    revoked: bool
    generation: int = 1
    path_length: int | None = None
    source: str = "v1"


@dataclass(frozen=True)
class V2AnchorRecord:
    anchor_id: str
    fingerprint: str
    subject_id: str
    effective_ms: int
    generation: int
    path_length: int
    trusted: bool
    tombstone: bool
    revoked: bool
    shard_id: str
    record_index: int
    source: str = "v2"


@dataclass(frozen=True)
class V1Snapshot:
    relative_effective_seconds: int
    trusted_root_ids: tuple[str, ...]
    revoked_root_ids: tuple[str, ...]
    no_path_length_constraint: bool
    anchors: tuple[V1AnchorRecord, ...]
    conversion: dict[str, Any]


@dataclass(frozen=True)
class V2Snapshot:
    generation: int
    effective_ms: int
    anchors: tuple[V2AnchorRecord, ...]
    shard_count: int
    metadata: dict[str, Any]


@dataclass
class TrustAnchor:
    fingerprint: str
    subject_id: str
    trusted: bool
    path_length: int | None
    generation: int
    effective_ms: int
    effective_low_ms: int
    effective_high_ms: int
    source: str
    source_record_ids: list[str] = field(default_factory=list)
    tombstone: bool = False
    revoked: bool = False
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    pending_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "subject_id": self.subject_id,
            "trusted": self.trusted,
            "path_length": self.path_length,
            "generation": self.generation,
            "effective_ms": self.effective_ms,
            "effective_low_ms": self.effective_low_ms,
            "effective_high_ms": self.effective_high_ms,
            "effective_interval": {
                "low_ms": self.effective_low_ms,
                "high_ms": self.effective_high_ms,
            },
            "source": self.source,
            "source_record_ids": list(self.source_record_ids),
            "tombstone": self.tombstone,
            "revoked": self.revoked,
            "conflicts": list(self.conflicts),
            "pending_confirmation": self.pending_confirmation,
        }


@dataclass
class TrustView:
    anchors: dict[str, TrustAnchor]
    conflicts: list[dict[str, Any]]
    snapshot_digest: str
    generation: int
    observation_clock_ms: int
    v1_quantization: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "certpath.trust-view",
            "version": 1,
            "snapshot_digest": self.snapshot_digest,
            "trust_generation": self.generation,
            "observation_clock_ms": self.observation_clock_ms,
            "v1_quantization": self.v1_quantization,
            "anchors": [
                self.anchors[key].to_dict() for key in sorted(self.anchors)
            ],
            "conflicts": self.conflicts,
            "pending_confirmation": sorted(
                key for key, value in self.anchors.items() if value.pending_confirmation
            ),
        }


@dataclass
class CandidatePath:
    certificate_ids: list[str]
    fingerprint_sequence: list[str]
    intermediate_count: int
    root_fingerprint: str
    root_source: str | None
    root_trust_generation: int | None
    status: str
    valid: bool
    pending_confirmation: bool
    business_codes: list[str]
    details: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "certificate_ids": list(self.certificate_ids),
            "fingerprint_sequence": list(self.fingerprint_sequence),
            "intermediate_count": self.intermediate_count,
            "root_fingerprint": self.root_fingerprint,
            "root_source": self.root_source,
            "trust_generation": self.root_trust_generation,
            "status": self.status,
            "valid": self.valid,
            "pending_confirmation": self.pending_confirmation,
            "business_codes": list(self.business_codes),
            "details": list(self.details),
        }


@dataclass
class ChainResult:
    chain_key: str
    chain_id: str
    declaration: list[str]
    conclusion: str
    selected_path: CandidatePath | None
    candidates: list[CandidatePath]
    candidates_total: int
    truncated: bool
    truncated_count: int
    truncated_has_valid: bool
    business_codes: list[str]
    invalidations: list[dict[str, Any]]
    remaining_available_paths: list[list[str]]
    enumeration_gaps: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "chain_key": self.chain_key,
            "chain_id": self.chain_id,
            "declaration": list(self.declaration),
            "conclusion": self.conclusion,
            "selected_path": (
                self.selected_path.to_dict() if self.selected_path is not None else None
            ),
            "selected_fingerprint_sequence": (
                list(self.selected_path.fingerprint_sequence)
                if self.selected_path is not None
                else None
            ),
            "trust_generation": (
                self.selected_path.root_trust_generation
                if self.selected_path is not None
                else None
            ),
            "candidates": [item.to_dict() for item in self.candidates],
            "candidate_fingerprint_sequences": [
                list(item.fingerprint_sequence) for item in self.candidates
            ],
            "candidates_total": self.candidates_total,
            "enumeration_truncated": self.truncated,
            "truncated_count": self.truncated_count,
            "truncated_has_valid": self.truncated_has_valid,
            "business_codes": list(self.business_codes),
            "invalidations": list(self.invalidations),
            "remaining_available_paths": list(self.remaining_available_paths),
            "enumeration_gaps": list(self.enumeration_gaps),
        }
