"""Dataclasses, normalization helpers and canonical JSON utilities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping


CERT_SCHEMA = "trustman.certificates/v1"
SNAPSHOT_SCHEMA = "trustman.snapshots/v1"
CHAIN_SCHEMA = "trustman.chains/v1"
REPORT_SCHEMA = "trustman.report/v1"
SNAPSHOT_STORE_SCHEMA = "trustman.snapshot-store/v1"
CHECKPOINT_SCHEMA = "trustman.checkpoint/v1"
CHECKPOINT_STRUCTURE_VERSION = 1

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
FINGERPRINT_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
POLICY_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?$")

CERT_FIELDS = {
    "id",
    "issuer_id",
    "public_key_fingerprint",
    "valid_from_ms",
    "valid_to_ms",
    "revoked",
}
SNAPSHOT_FIELDS = {
    "version",
    "policy_version",
    "trusted_roots",
    "revoked_entries",
    "observed_at_ms",
}
REVOCATION_FIELDS = {"cert_id", "revoked_at_ms", "reason"}
CHAIN_FIELDS = {
    "chain_id",
    "certificate_ids",
    "deadline_ms",
    "expected_summary",
}


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def sha256_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(value)).hexdigest()


def sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def is_strict_int(value: Any) -> bool:
    return type(value) is int


def normalized_fingerprint(value: str) -> str:
    return value.lower()


def normalized_ids(values: Iterable[str]) -> list[str]:
    return sorted(values)


@dataclass(frozen=True)
class Rejection:
    scope: str
    source_file: str
    entry_index: int
    code: str
    reason: str
    message: str
    chain_id: str | None = None

    @property
    def entry_number(self) -> int:
        return self.entry_index + 1 if self.entry_index >= 0 else 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "source_file": self.source_file,
            "entry_index": self.entry_index,
            "entry_number": self.entry_number,
            "code": self.code,
            "reason": self.reason,
            "message": self.message,
            "chain_id": self.chain_id,
        }

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.scope,
            self.source_file,
            self.entry_index,
            self.code,
            self.reason,
            self.chain_id or "",
            self.message,
        )


@dataclass(frozen=True)
class Certificate:
    cert_id: str
    issuer_id: str
    public_key_fingerprint: str
    valid_from_ms: int
    valid_to_ms: int
    revoked: bool
    source_file: str
    entry_index: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.cert_id,
            "issuer_id": self.issuer_id,
            "public_key_fingerprint": self.public_key_fingerprint,
            "valid_from_ms": self.valid_from_ms,
            "valid_to_ms": self.valid_to_ms,
            "revoked": self.revoked,
            "source_file": self.source_file,
            "entry_index": self.entry_index,
        }

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "id": self.cert_id,
            "issuer_id": self.issuer_id,
            "public_key_fingerprint": self.public_key_fingerprint,
            "valid_from_ms": self.valid_from_ms,
            "valid_to_ms": self.valid_to_ms,
            "revoked": self.revoked,
        }


@dataclass(frozen=True)
class RevocationEntry:
    cert_id: str
    revoked_at_ms: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "cert_id": self.cert_id,
            "revoked_at_ms": self.revoked_at_ms,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class Snapshot:
    version: int
    policy_version: str
    trusted_roots: tuple[str, ...]
    revoked_entries: tuple[RevocationEntry, ...]
    observed_at_ms: int
    content_fingerprint: str
    source_file: str
    entry_index: int

    @property
    def revoked_by_id(self) -> dict[str, RevocationEntry]:
        return {entry.cert_id: entry for entry in self.revoked_entries}

    def as_dict(self, include_source: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "version": self.version,
            "policy_version": self.policy_version,
            "trusted_roots": list(self.trusted_roots),
            "revoked_entries": [entry.as_dict() for entry in self.revoked_entries],
            "observed_at_ms": self.observed_at_ms,
            "content_fingerprint": self.content_fingerprint,
        }
        if include_source:
            value["source_file"] = self.source_file
            value["entry_index"] = self.entry_index
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "policy_version": self.policy_version,
            "trusted_roots": list(self.trusted_roots),
            "revoked_entries": [entry.as_dict() for entry in self.revoked_entries],
            "observed_at_ms": self.observed_at_ms,
        }


@dataclass(frozen=True)
class ChainDeclaration:
    chain_id: str
    certificate_ids: tuple[str, ...]
    deadline_ms: int
    expected_summary: str
    source_file: str
    entry_index: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "certificate_ids": list(self.certificate_ids),
            "deadline_ms": self.deadline_ms,
            "expected_summary": self.expected_summary,
            "source_file": self.source_file,
            "entry_index": self.entry_index,
        }

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "chain_id": self.chain_id,
            "certificate_ids": list(self.certificate_ids),
            "deadline_ms": self.deadline_ms,
            "expected_summary": self.expected_summary,
        }

    @property
    def sort_key(self) -> tuple[str, str, int]:
        return (self.chain_id, self.source_file, self.entry_index)


def snapshot_content_fingerprint(
    *,
    version: int,
    policy_version: str,
    trusted_roots: Iterable[str],
    revoked_entries: Iterable[RevocationEntry],
    observed_at_ms: int,
) -> str:
    return sha256_value(
        {
            "version": version,
            "policy_version": policy_version,
            "trusted_roots": sorted(trusted_roots),
            "revoked_entries": [
                entry.as_dict()
                for entry in sorted(revoked_entries, key=lambda item: item.cert_id)
            ],
            "observed_at_ms": observed_at_ms,
        }
    )


def certificate_map(certificates: Iterable[Certificate]) -> dict[str, Certificate]:
    return {certificate.cert_id: certificate for certificate in certificates}


def effective_snapshot(snapshots: Iterable[Snapshot]) -> Snapshot | None:
    ordered = sorted(snapshots, key=lambda item: (item.version, item.content_fingerprint))
    return ordered[-1] if ordered else None


def snapshot_report_dict(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "version": snapshot.version,
        "policy_version": snapshot.policy_version,
        "trusted_roots": list(snapshot.trusted_roots),
        "revoked_entries": [entry.as_dict() for entry in snapshot.revoked_entries],
        "observed_at_ms": snapshot.observed_at_ms,
        "content_fingerprint": snapshot.content_fingerprint,
    }


def load_json_object_from_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    """Defensive copy for callers receiving decoded JSON objects."""
    return json.loads(canonical_json(dict(value)))
