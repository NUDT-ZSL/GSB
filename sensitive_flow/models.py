"""Serializable state models used by replay and checkpoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@dataclass
class FieldState:
    key: str
    path: str
    level: int
    masked: bool
    first_version: int
    last_version: int
    entry_index: int
    deleted: bool = False
    deleted_version: int | None = None
    path_history: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "path": self.path,
            "level": self.level,
            "masked": self.masked,
            "first_version": self.first_version,
            "last_version": self.last_version,
            "entry_index": self.entry_index,
            "deleted": self.deleted,
            "deleted_version": self.deleted_version,
            "path_history": list(self.path_history),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FieldState":
        return cls(
            key=value["key"],
            path=value["path"],
            level=value["level"],
            masked=value["masked"],
            first_version=value["first_version"],
            last_version=value["last_version"],
            entry_index=value["entry_index"],
            deleted=bool(value.get("deleted", False)),
            deleted_version=_optional_int(value.get("deleted_version")),
            path_history=list(value.get("path_history", [])),
        )


@dataclass
class DerivationState:
    id: str
    source: str
    target: str
    version: str
    transitive: bool
    created_version: int
    entry_index: int
    source_path: str
    target_path: str
    version_sync: dict[str, Any] = field(default_factory=dict)

    @property
    def propagation_enabled(self) -> bool:
        return self.version == "v2" and self.transitive

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "target": self.target,
            "version": self.version,
            "transitive": self.transitive,
            "propagation_enabled": self.propagation_enabled,
            "created_version": self.created_version,
            "entry_index": self.entry_index,
            "source_path": self.source_path,
            "target_path": self.target_path,
            "version_sync": dict(self.version_sync),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DerivationState":
        return cls(
            id=value["id"],
            source=value["source"],
            target=value["target"],
            version=value["version"],
            transitive=bool(value["transitive"]),
            created_version=value["created_version"],
            entry_index=value.get("entry_index", 0),
            source_path=value.get("source_path", value["source"]),
            target_path=value.get("target_path", value["target"]),
            version_sync=dict(value.get("version_sync", {})),
        )


@dataclass
class Rejection:
    document_id: str
    item_index: int
    code: str
    message: str
    item_type: str
    entry_number: int | None = None
    path: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        number = self.entry_number if self.entry_number is not None else self.item_index + 1
        result: dict[str, Any] = {
            "document_id": self.document_id,
            "item_index": self.item_index,
            "entry_number": number,
            "item_type": self.item_type,
            "code": self.code,
            "message": self.message,
        }
        if self.path is not None:
            result["path"] = self.path
        if self.details:
            result["details"] = dict(self.details)
        return result

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Rejection":
        return cls(
            document_id=value["document_id"],
            item_index=value["item_index"],
            code=value["code"],
            message=value["message"],
            item_type=value.get("item_type", "unknown"),
            entry_number=value.get("entry_number"),
            path=value.get("path"),
            details=dict(value.get("details", {})),
        )


@dataclass
class DocumentState:
    document_id: str
    latest_version: int | None = None
    latest_version_raw: str | None = None
    applied_versions: list[int] = field(default_factory=list)
    fields: dict[str, FieldState] = field(default_factory=dict)
    tombstones: dict[str, FieldState] = field(default_factory=dict)
    derivations: dict[str, DerivationState] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "latest_version": self.latest_version,
            "latest_version_raw": self.latest_version_raw,
            "applied_versions": list(self.applied_versions),
            "fields": {key: self.fields[key].to_dict() for key in sorted(self.fields)},
            "tombstones": {
                key: self.tombstones[key].to_dict() for key in sorted(self.tombstones)
            },
            "derivations": {
                key: self.derivations[key].to_dict() for key in sorted(self.derivations)
            },
            "rejections": [item.to_dict() for item in self.rejections],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DocumentState":
        state = cls(
            document_id=value["document_id"],
            latest_version=_optional_int(value.get("latest_version")),
            latest_version_raw=value.get("latest_version_raw"),
            applied_versions=list(value.get("applied_versions", [])),
        )
        state.fields = {
            key: FieldState.from_dict(item) for key, item in value.get("fields", {}).items()
        }
        state.tombstones = {
            key: FieldState.from_dict(item)
            for key, item in value.get("tombstones", {}).items()
        }
        state.derivations = {
            key: DerivationState.from_dict(item)
            for key, item in value.get("derivations", {}).items()
        }
        state.rejections = [
            Rejection.from_dict(item) for item in value.get("rejections", [])
        ]
        return state


@dataclass
class TraceState:
    documents: dict[str, DocumentState] = field(default_factory=dict)
    processed_edits: int = 0
    last_edit_seq: int = 0
    edit_log: list[dict[str, Any]] = field(default_factory=list)

    def document(self, document_id: str, create: bool = False) -> DocumentState | None:
        result = self.documents.get(document_id)
        if result is None and create:
            result = DocumentState(document_id)
            self.documents[document_id] = result
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "documents": {
                key: self.documents[key].to_dict() for key in sorted(self.documents)
            },
            "processed_edits": self.processed_edits,
            "last_edit_seq": self.last_edit_seq,
            "edit_log": [dict(item) for item in self.edit_log],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TraceState":
        state = cls(
            processed_edits=value.get("processed_edits", 0),
            last_edit_seq=value.get("last_edit_seq", 0),
            edit_log=[dict(item) for item in value.get("edit_log", [])],
        )
        state.documents = {
            key: DocumentState.from_dict(item)
            for key, item in value.get("documents", {}).items()
        }
        return state
