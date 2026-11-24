"""Typed data structures shared by the parser, engine and report writer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from . import codes


def canonical_json(value: Any) -> str:
    """Canonical JSON used for reproducible hashes."""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def digest_value(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def strip_source_paths(value: Any) -> Any:
    """Remove display-only source paths before computing stable digests."""

    if isinstance(value, dict):
        return {
            key: strip_source_paths(item)
            for key, item in value.items()
            if key != "source_file"
        }
    if isinstance(value, list):
        return [strip_source_paths(item) for item in value]
    return value


@dataclass
class Diagnostic:
    code: str
    message: str
    severity: str = "error"
    source_file: Optional[str] = None
    record_index: Optional[int] = None
    connection_id: Optional[str] = None
    message_seq: Optional[int] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
        }
        if self.source_file is not None:
            out["source_file"] = self.source_file
        if self.record_index is not None:
            out["record_index"] = self.record_index
        if self.connection_id is not None:
            out["connection_id"] = self.connection_id
        if self.message_seq is not None:
            out["message_seq"] = self.message_seq
        if self.detail:
            out["detail"] = self.detail
        return out

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Diagnostic":
        return cls(
            code=str(data.get("code", codes.E_INTERNAL)),
            message=str(data.get("message", "")),
            severity=str(data.get("severity", "error")),
            source_file=data.get("source_file"),
            record_index=data.get("record_index"),
            connection_id=data.get("connection_id"),
            message_seq=data.get("message_seq"),
            detail=dict(data.get("detail") or {}),
        )


@dataclass(frozen=True)
class RawRecord:
    connection_id: str
    direction: str
    record_seq: int
    timestamp_ms: int
    record_type: str
    payload_hex: str
    payload: bytes
    source_file: str
    line_no: int
    arrival_index: int
    embedded_connection_id: Optional[str] = None


@dataclass(frozen=True)
class FragmentEnvelope:
    record: RawRecord
    message_id: int
    kind: int
    declared_length: int
    offset: int
    stream_tag: str
    data: bytes

    @property
    def end_offset(self) -> int:
        return self.offset + len(self.data)

    @property
    def kind_name(self) -> str:
        if self.kind == 1:
            return "certificate"
        if self.kind == 2:
            return "handshake"
        return "other"


@dataclass
class ReassembledMessage:
    message_id: int
    kind: str
    data: bytes
    declared_length: int
    first_seq: int
    last_seq: int
    direction: str
    record_seqs: List[int] = field(default_factory=list)
    payload: Optional[Dict[str, Any]] = None
    length_ok: bool = True
    stream_tag: Optional[str] = None

    def summary(self) -> Dict[str, Any]:
        return {
            "message_id": self.message_id,
            "kind": self.kind,
            "first_record_seq": self.first_seq,
            "last_record_seq": self.last_seq,
            "record_count": len(self.record_seqs),
            "declared_length": self.declared_length,
            "expanded_length": len(self.data),
            "length_ok": self.length_ok,
            "direction": self.direction,
        }


@dataclass
class ReassemblyResult:
    status: str = "ok"
    messages: List[ReassembledMessage] = field(default_factory=list)
    issues: List[Diagnostic] = field(default_factory=list)
    missing_sequence_ranges: List[Tuple[int, int]] = field(default_factory=list)
    missing_byte_ranges: Dict[int, List[Tuple[int, int]]] = field(
        default_factory=dict
    )
    duplicate_count: int = 0
    duplicate_sequences: List[int] = field(default_factory=list)
    duplicate_conflicts: int = 0
    fragment_count: int = 0
    unique_fragment_count: int = 0
    expanded_bytes: int = 0
    mixup_records: List[int] = field(default_factory=list)
    envelope_mode: bool = False
    complete: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "complete": self.complete,
            "envelope_mode": self.envelope_mode,
            "fragment_count": self.fragment_count,
            "unique_fragment_count": self.unique_fragment_count,
            "expanded_bytes": self.expanded_bytes,
            "duplicate_count": self.duplicate_count,
            "duplicate_sequences": list(self.duplicate_sequences),
            "duplicate_conflicts": self.duplicate_conflicts,
            "missing_sequence_ranges": [list(x) for x in self.missing_sequence_ranges],
            "missing_byte_ranges": {
                str(k): [list(x) for x in v]
                for k, v in sorted(self.missing_byte_ranges.items())
            },
            "mixup_records": list(self.mixup_records),
            "messages": [m.summary() for m in self.messages],
            "issues": [i.to_dict() for i in self.issues],
        }


def normalize_fingerprint(value: Any) -> Optional[str]:
    """Return ``sha256:<lowercase hex>`` or ``None`` for an invalid value.

    The canonical form is a lower-case SHA-256 digest.  Bare 64-hex values are
    accepted for interoperability, but snapshots are always reported in the
    canonical prefixed form.
    """

    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.lower()
    if lowered.startswith("sha256:"):
        hex_part = lowered[7:]
    elif lowered.startswith("sha-256:"):
        hex_part = lowered[8:]
    else:
        hex_part = lowered
    if len(hex_part) != 64:
        return None
    if any(ch not in "0123456789abcdef" for ch in hex_part):
        return None
    return "sha256:" + hex_part


def normalize_alias(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip().rstrip(".").strip()
    if not text or len(text) > 253:
        return None
    if any(ord(ch) < 32 or ch.isspace() for ch in text):
        return None
    import unicodedata

    return unicodedata.normalize("NFKC", text).casefold()


@dataclass
class TrustSourceRecord:
    fingerprint: str
    alias: str
    canonical_alias: Optional[str]
    enabled: bool
    effective_ms: int
    generation: int
    tombstone: bool
    revoked: bool
    revoked_at_ms: Optional[int]
    source_generation: str  # "v1" or "v2"
    source_file: str
    record_index: int
    raw: Dict[str, Any] = field(default_factory=dict)

    def source_id(self) -> str:
        return f"{self.source_generation}:{self.source_file}:{self.record_index}"

    def key_material(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "alias": self.canonical_alias or self.alias,
            "enabled": self.enabled,
            "effective_ms": self.effective_ms,
            "generation": self.generation,
            "tombstone": self.tombstone,
            "revoked": self.revoked,
            "revoked_at_ms": self.revoked_at_ms,
            "source_generation": self.source_generation,
            "source_file": self.source_file,
            "record_index": self.record_index,
        }

    def to_dict(self) -> Dict[str, Any]:
        return self.key_material()


@dataclass
class TrustConflict:
    fingerprint: str
    aliases: List[str]
    fields: List[str]
    authoritative_source: str
    authoritative_generation: int
    sources: List[Dict[str, Any]]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "aliases": list(self.aliases),
            "fields": list(self.fields),
            "authoritative_source": self.authoritative_source,
            "authoritative_generation": self.authoritative_generation,
            "sources": list(self.sources),
        }


@dataclass
class TrustEntity:
    fingerprint: str
    canonical_alias: str
    aliases: List[str]
    enabled: bool
    effective_ms: int
    generation: int
    tombstone: bool
    revoked: bool
    revoked_at_ms: Optional[int]
    authoritative_source: str
    sources: List[TrustSourceRecord] = field(default_factory=list)
    tombstone_covers: List[str] = field(default_factory=list)
    conflict_fields: List[str] = field(default_factory=list)

    @property
    def trusted(self) -> bool:
        return self.enabled and not self.tombstone and not self.revoked

    def effective_revoked(self, observed_ms: Optional[int] = None) -> bool:
        if self.tombstone or not self.revoked:
            return False
        if self.revoked_at_ms is None or observed_ms is None:
            return True
        return self.revoked_at_ms <= observed_ms

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "canonical_alias": self.canonical_alias,
            "aliases": sorted(set(self.aliases)),
            "enabled": self.enabled,
            "effective_ms": self.effective_ms,
            "generation": self.generation,
            "tombstone": self.tombstone,
            "revoked": self.revoked,
            "revoked_at_ms": self.revoked_at_ms,
            "trusted": self.trusted,
            "authoritative_source": self.authoritative_source,
            "tombstone_covers": sorted(self.tombstone_covers),
            "conflict_fields": sorted(self.conflict_fields),
            "sources": [s.to_dict() for s in sorted(self.sources, key=lambda x: x.source_id())],
        }


@dataclass
class TrustView:
    entities: Dict[str, TrustEntity] = field(default_factory=dict)
    conflicts: List[TrustConflict] = field(default_factory=list)
    diagnostics: List[Diagnostic] = field(default_factory=list)
    source_versions: Dict[str, str] = field(default_factory=dict)
    source_summaries: Dict[str, Any] = field(default_factory=dict)
    version: str = ""
    policy_version: str = codes.TRUST_MERGE_POLICY_VERSION

    @property
    def trusted(self) -> Dict[str, TrustEntity]:
        return {k: v for k, v in self.entities.items() if v.trusted}

    @property
    def tombstones(self) -> List[TrustEntity]:
        return [e for e in self.entities.values() if e.tombstone]

    @property
    def revocations(self) -> List[TrustEntity]:
        return [e for e in self.entities.values() if e.revoked]

    def trusted_summary(self) -> List[Dict[str, Any]]:
        return [
            {
                "fingerprint": e.fingerprint,
                "alias": e.canonical_alias,
                "enabled": e.enabled,
                "effective_ms": e.effective_ms,
                "generation": e.generation,
                "tombstone": e.tombstone,
                "revoked": e.revoked,
                "trusted": e.trusted,
            }
            for e in sorted(self.entities.values(), key=lambda x: x.fingerprint)
        ]

    def summary(self) -> Dict[str, Any]:
        return {
            "snapshot_version": self.version,
            "merge_policy_version": self.policy_version,
            "source_versions": dict(sorted(self.source_versions.items())),
            "source_summaries": self.source_summaries,
            "entity_count": len(self.entities),
            "trusted_count": len(self.trusted),
            "tombstone_count": len(self.tombstones),
            "revoked_count": len(self.revocations),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "entities": [
                e.to_dict() for e in sorted(self.entities.values(), key=lambda x: x.fingerprint)
            ],
            "tombstones": [
                e.to_dict()
                for e in sorted(self.tombstones, key=lambda x: x.fingerprint)
            ],
            "revocations": [
                e.to_dict()
                for e in sorted(self.revocations, key=lambda x: x.fingerprint)
            ],
            "trusted_summary": self.trusted_summary(),
        }


@dataclass
class Certificate:
    index: int
    subject: str
    issuer: str
    serial: str
    not_before_ms: int
    not_after_ms: int
    public_key_fingerprint: str
    signature: str
    revoked: bool = False
    revoked_at_ms: Optional[int] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "subject": self.subject,
            "issuer": self.issuer,
            "serial": self.serial,
            "not_before_ms": self.not_before_ms,
            "not_after_ms": self.not_after_ms,
            "public_key_fingerprint": self.public_key_fingerprint,
            "signature": self.signature,
            "revoked": self.revoked,
            "revoked_at_ms": self.revoked_at_ms,
        }


@dataclass
class ChainEvaluation:
    candidate_id: str
    certificates: List[Certificate]
    fingerprints: List[str]
    root_fingerprint: Optional[str]
    message_seq: Optional[int]
    issues: List[Diagnostic] = field(default_factory=list)
    valid: bool = False
    trusted: bool = False
    root_generation: int = -1
    root_effective_ms: int = -1
    root_alias: Optional[str] = None
    root_status: str = "missing"
    revoked_roles: List[Dict[str, Any]] = field(default_factory=list)
    expired_roles: List[Dict[str, Any]] = field(default_factory=list)
    conclusion: str = "untrusted"
    primary_code: Optional[str] = None
    rank: int = 0
    score: Tuple[Any, ...] = ()

    def to_dict(self, selected: bool = False) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "selected": selected,
            "rank": self.rank,
            "trusted": self.trusted,
            "valid": self.valid,
            "conclusion": self.conclusion,
            "primary_code": self.primary_code,
            "root_fingerprint": self.root_fingerprint,
            "root_alias": self.root_alias,
            "root_generation": self.root_generation,
            "root_effective_ms": self.root_effective_ms,
            "root_status": self.root_status,
            "message_seq": self.message_seq,
            "fingerprints": list(self.fingerprints),
            "certificates": [c.to_dict() for c in self.certificates],
            "revoked_roles": list(self.revoked_roles),
            "expired_roles": list(self.expired_roles),
            "codes": sorted({i.code for i in self.issues}),
            "issues": [i.to_dict() for i in self.issues],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ChainEvaluation":
        certs: List[Certificate] = []
        for raw in data.get("certificates", []) or []:
            certs.append(
                Certificate(
                    index=int(raw.get("index", 0)),
                    subject=str(raw.get("subject", "")),
                    issuer=str(raw.get("issuer", "")),
                    serial=str(raw.get("serial", "")),
                    not_before_ms=int(raw.get("not_before_ms", 0)),
                    not_after_ms=int(raw.get("not_after_ms", 0)),
                    public_key_fingerprint=str(raw.get("public_key_fingerprint", "")),
                    signature=str(raw.get("signature", "")),
                    revoked=bool(raw.get("revoked", False)),
                    revoked_at_ms=raw.get("revoked_at_ms"),
                    raw=dict(raw),
                )
            )
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            certificates=certs,
            fingerprints=[str(x) for x in data.get("fingerprints", []) or []],
            root_fingerprint=data.get("root_fingerprint"),
            message_seq=data.get("message_seq"),
            issues=[Diagnostic.from_dict(x) for x in data.get("issues", []) or []],
            valid=bool(data.get("valid", False)),
            trusted=bool(data.get("trusted", False)),
            root_generation=int(data.get("root_generation", -1)),
            root_effective_ms=int(data.get("root_effective_ms", -1)),
            root_alias=data.get("root_alias"),
            root_status=str(data.get("root_status", "missing")),
            revoked_roles=list(data.get("revoked_roles", []) or []),
            expired_roles=list(data.get("expired_roles", []) or []),
            conclusion=str(data.get("conclusion", "untrusted")),
            primary_code=data.get("primary_code"),
            rank=int(data.get("rank", 0)),
        )


@dataclass
class ConnectionResult:
    connection_id: str
    status: str = "ok"
    record_count: int = 0
    directions: List[str] = field(default_factory=list)
    reassembly: Optional[ReassemblyResult] = None
    observed_time_ms: Optional[int] = None
    clock_source: Optional[str] = None
    selected_chain: Optional[ChainEvaluation] = None
    candidate_chains: List[ChainEvaluation] = field(default_factory=list)
    trust_conclusion: str = "untrusted"
    primary_code: Optional[str] = None
    issues: List[Diagnostic] = field(default_factory=list)
    revocation_propagation: List[Dict[str, Any]] = field(default_factory=list)
    elapsed_ms: float = 0.0
    timeout_ms: int = 0
    budget: Dict[str, Any] = field(default_factory=dict)
    selected_chain_digest: Optional[str] = None
    conclusion_digest: str = ""

    def stable_payload(self) -> Dict[str, Any]:
        """Fields that must be identical across replay and concurrency modes."""

        payload = {
            "connection_id": self.connection_id,
            "status": self.status,
            "record_count": self.record_count,
            "directions": sorted(self.directions),
            "reassembly": self.reassembly.to_dict() if self.reassembly else None,
            "observed_time_ms": self.observed_time_ms,
            "clock_source": self.clock_source,
            "selected_chain": (
                self.selected_chain.to_dict(selected=True)
                if self.selected_chain
                else None
            ),
            "candidate_chains": [
                c.to_dict(selected=(self.selected_chain is not None and c is self.selected_chain))
                for c in self.candidate_chains
            ],
            "trust_conclusion": self.trust_conclusion,
            "primary_code": self.primary_code,
            "issues": [i.to_dict() for i in self.issues],
            "revocation_propagation": list(self.revocation_propagation),
            "budget": dict(self.budget),
        }
        return strip_source_paths(payload)

    def compute_digest(self) -> str:
        self.conclusion_digest = digest_value(self.stable_payload())
        if self.selected_chain is not None:
            self.selected_chain_digest = digest_value(self.selected_chain.to_dict(selected=True))
        else:
            self.selected_chain_digest = None
        return self.conclusion_digest

    def to_dict(self) -> Dict[str, Any]:
        reassembly = self.reassembly.to_dict() if self.reassembly else None
        selected = self.selected_chain.to_dict(selected=True) if self.selected_chain else None
        candidates = [
            c.to_dict(selected=(self.selected_chain is not None and c is self.selected_chain))
            for c in self.candidate_chains
        ]
        return {
            "connection_id": self.connection_id,
            "status": self.status,
            "record_count": self.record_count,
            "directions": sorted(self.directions),
            "fragment_reassembly": reassembly,
            "observed_time_ms": self.observed_time_ms,
            "clock_source": self.clock_source,
            "selected_chain": selected,
            "candidate_chains": candidates,
            "trust_conclusion": self.trust_conclusion,
            "primary_code": self.primary_code,
            "issues": [i.to_dict() for i in self.issues],
            "revocation_propagation": list(self.revocation_propagation),
            "elapsed_ms": round(self.elapsed_ms, 3),
            "timeout_ms": self.timeout_ms,
            "budget": dict(self.budget),
            "conclusion_digest": self.conclusion_digest,
            "selected_chain_digest": self.selected_chain_digest,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConnectionResult":
        reassembly_raw = data.get("fragment_reassembly")
        reassembly = None
        if reassembly_raw:
            restored_messages: List[ReassembledMessage] = []
            for message_raw in reassembly_raw.get("messages", []) or []:
                expanded_length = int(message_raw.get("expanded_length", 0))
                record_count = int(message_raw.get("record_count", 0))
                restored_messages.append(
                    ReassembledMessage(
                        message_id=int(message_raw.get("message_id", 0)),
                        kind=str(message_raw.get("kind", "other")),
                        data=b"\0" * expanded_length,
                        declared_length=int(message_raw.get("declared_length", 0)),
                        first_seq=int(message_raw.get("first_record_seq", 0)),
                        last_seq=int(message_raw.get("last_record_seq", 0)),
                        direction=str(message_raw.get("direction", "")),
                        record_seqs=[0] * record_count,
                        payload=None,
                        length_ok=bool(message_raw.get("length_ok", False)),
                        stream_tag=None,
                    )
                )
            reassembly = ReassemblyResult(
                status=str(reassembly_raw.get("status", "ok")),
                messages=restored_messages,
                issues=[
                    Diagnostic.from_dict(x)
                    for x in reassembly_raw.get("issues", []) or []
                ],
                missing_sequence_ranges=[
                    tuple(x) for x in reassembly_raw.get("missing_sequence_ranges", []) or []
                ],
                missing_byte_ranges={
                    int(k): [tuple(y) for y in v]
                    for k, v in (reassembly_raw.get("missing_byte_ranges") or {}).items()
                },
                duplicate_count=int(reassembly_raw.get("duplicate_count", 0)),
                duplicate_sequences=[
                    int(x) for x in reassembly_raw.get("duplicate_sequences", []) or []
                ],
                duplicate_conflicts=int(reassembly_raw.get("duplicate_conflicts", 0)),
                fragment_count=int(reassembly_raw.get("fragment_count", 0)),
                unique_fragment_count=int(reassembly_raw.get("unique_fragment_count", 0)),
                expanded_bytes=int(reassembly_raw.get("expanded_bytes", 0)),
                mixup_records=[int(x) for x in reassembly_raw.get("mixup_records", []) or []],
                envelope_mode=bool(reassembly_raw.get("envelope_mode", False)),
                complete=bool(reassembly_raw.get("complete", True)),
            )
        selected_raw = data.get("selected_chain")
        selected = ChainEvaluation.from_dict(selected_raw) if selected_raw else None
        candidates = [
            ChainEvaluation.from_dict(x) for x in data.get("candidate_chains", []) or []
        ]
        if selected is not None:
            selected = next(
                (
                    candidate
                    for candidate in candidates
                    if candidate.candidate_id == selected.candidate_id
                ),
                selected,
            )
        result = cls(
            connection_id=str(data.get("connection_id", "")),
            status=str(data.get("status", "ok")),
            record_count=int(data.get("record_count", 0)),
            directions=[str(x) for x in data.get("directions", []) or []],
            reassembly=reassembly,
            observed_time_ms=data.get("observed_time_ms"),
            clock_source=data.get("clock_source"),
            selected_chain=selected,
            candidate_chains=candidates,
            trust_conclusion=str(data.get("trust_conclusion", "untrusted")),
            primary_code=data.get("primary_code"),
            issues=[Diagnostic.from_dict(x) for x in data.get("issues", []) or []],
            revocation_propagation=list(data.get("revocation_propagation", []) or []),
            elapsed_ms=float(data.get("elapsed_ms", 0.0)),
            timeout_ms=int(data.get("timeout_ms", 0)),
            budget=dict(data.get("budget") or {}),
            selected_chain_digest=data.get("selected_chain_digest"),
            conclusion_digest=str(data.get("conclusion_digest", "")),
        )
        return result


@dataclass
class AnalysisConfig:
    jobs: int = 1
    timeout_ms: int = 5000
    max_frags_per_connection: int = 10000
    max_expanded_bytes_per_connection: int = 16 * 1024 * 1024
    max_total_expanded_bytes: int = 256 * 1024 * 1024
    lock_timeout_ms: int = 0
    resume: bool = False
    checkpoint_every_connection: bool = True

    def __post_init__(self) -> None:
        self.jobs = max(1, int(self.jobs))
        self.timeout_ms = max(0, int(self.timeout_ms))
        self.max_frags_per_connection = max(0, int(self.max_frags_per_connection))
        self.max_expanded_bytes_per_connection = max(
            0, int(self.max_expanded_bytes_per_connection)
        )
        self.max_total_expanded_bytes = max(0, int(self.max_total_expanded_bytes))
        self.lock_timeout_ms = max(0, int(self.lock_timeout_ms))

    def budget_fingerprint_material(self) -> Dict[str, Any]:
        return {
            "timeout_ms": self.timeout_ms,
            "max_frags_per_connection": self.max_frags_per_connection,
            "max_expanded_bytes_per_connection": self.max_expanded_bytes_per_connection,
            "max_total_expanded_bytes": self.max_total_expanded_bytes,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "jobs": self.jobs,
            "timeout_ms": self.timeout_ms,
            "max_frags_per_connection": self.max_frags_per_connection,
            "max_expanded_bytes_per_connection": self.max_expanded_bytes_per_connection,
            "max_total_expanded_bytes": self.max_total_expanded_bytes,
            "lock_timeout_ms": self.lock_timeout_ms,
            "resume": self.resume,
            "checkpoint_every_connection": self.checkpoint_every_connection,
        }
