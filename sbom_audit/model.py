"""Shared immutable data models and deterministic JSON helpers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
from typing import Any, Iterable


class AuditError(ValueError):
    """An error carrying a stable business code."""

    def __init__(self, code: str, message: str = "", field: str | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.field = field


@dataclasses.dataclass(frozen=True)
class ComponentRecord:
    canonical_key: str
    component_id: str
    version: str | None
    algorithm: str | None
    digest: str | None
    signature_status: str
    published_at_ms: int | None
    generation: int
    snapshot_kind: str
    snapshot_id: str
    source_ref: str
    record_index: int
    tombstone: bool = False
    revoked: bool = False
    revoked_at_ms: int | None = None
    shard: str | None = None
    delivery_id: str | None = None
    payload_file: str | None = None
    sync_marker: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass
class Rejection:
    snapshot: str
    index: int
    code: str
    message: str
    field: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "snapshot": self.snapshot,
            "index": self.index,
            "code": self.code,
            "message": self.message,
        }
        if self.field:
            data["field"] = self.field
        return data


@dataclasses.dataclass
class ParseResult:
    source_name: str
    kind: str
    snapshot_id: str
    generation: int | None
    records: list[ComponentRecord] = dataclasses.field(default_factory=list)
    rejections: list[Rejection] = dataclasses.field(default_factory=list)
    observation_clock_ms: int | None = None
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "kind": self.kind,
            "snapshot_id": self.snapshot_id,
            "generation": self.generation,
            "records": [r.to_dict() for r in self.records],
            "rejections": [r.to_dict() for r in self.rejections],
            "observation_clock_ms": self.observation_clock_ms,
            "metadata": self.metadata,
        }


_KEY_RE = re.compile(r"^pkg:[a-z0-9][a-z0-9+._-]*/[A-Za-z0-9][A-Za-z0-9._/@-]*$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def canonical_key(component_id: str, ecosystem: str | None = None) -> str:
    """Return the canonical entity key for a component identifier."""

    if not isinstance(component_id, str) or not component_id.strip():
        raise AuditError("E_INVALID_ID", "component id must be a non-empty string", "id")
    raw = component_id.strip()
    if raw.startswith("pkg:"):
        key = raw
    else:
        eco = (ecosystem or "generic").strip().lower()
        if not re.match(r"^[a-z0-9][a-z0-9+._-]*$", eco):
            raise AuditError("E_INVALID_ID", f"invalid ecosystem: {ecosystem!r}", "ecosystem")
        key = f"pkg:{eco}/{raw}"
    if not _KEY_RE.match(key) or len(key) > 300:
        raise AuditError("E_INVALID_ID", f"invalid canonical key: {key!r}", "canonical_key")
    return key


def valid_component_id(component_id: Any) -> bool:
    return isinstance(component_id, str) and bool(_ID_RE.match(component_id)) and len(component_id) <= 200


def canonical_json(value: Any) -> str:
    """Stable JSON used for digests and field-for-field comparisons."""

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def digest_object(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def sorted_dicts(items: Iterable[dict[str, Any]], key_field: str = "canonical_key") -> list[dict[str, Any]]:
    return sorted(items, key=lambda item: item.get(key_field, ""))
