"""Versioned, checksummed snapshots with atomic whole replacement."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_SNAPSHOT_UNKNOWN,
    STAGE_SNAPSHOT,
    Diagnostic,
)
from .util import canonical_json_bytes, sha256_json


def _snapshot_payload(category: str, version: int, content: Any) -> dict[str, Any]:
    return {
        "category": category,
        "version": version,
        "content": content,
    }


@dataclass(frozen=True)
class Snapshot:
    category: str
    version: int
    content: Any
    checksum: str

    @classmethod
    def create(
        cls,
        category: str,
        version: int,
        content: Any,
        checksum: str | None = None,
    ) -> "Snapshot":
        computed = sha256_json(_snapshot_payload(category, version, content))
        return cls(
            category=category,
            version=int(version),
            content=copy.deepcopy(content),
            checksum=checksum if checksum is not None else computed,
        )

    def computed_checksum(self) -> str:
        return sha256_json(_snapshot_payload(self.category, self.version, self.content))

    def is_valid(self) -> bool:
        return isinstance(self.category, str) and self.category != "" and (
            isinstance(self.version, int)
            and not isinstance(self.version, bool)
            and self.version > 0
            and self.checksum == self.computed_checksum()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "version": self.version,
            "checksum": self.checksum,
            "content": copy.deepcopy(self.content),
        }


@dataclass(frozen=True)
class SnapshotOperation:
    ok: bool
    code: str | None = None
    snapshot: Snapshot | None = None
    error: dict[str, Any] | None = None

    def to_dict(self, include_content: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": self.ok,
            "code": self.code,
            "error": copy.deepcopy(self.error),
        }
        if self.snapshot is not None:
            snapshot = self.snapshot.to_dict()
            if not include_content:
                snapshot.pop("content", None)
            result["snapshot"] = snapshot
        return result


def _snapshot_error(
    code: str,
    category: str,
    version: int | None,
    message: str,
    details: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    diagnostic = Diagnostic(
        code=code,
        stage=f"{STAGE_SNAPSHOT}:{category}",
        job_id="",
        field="snapshot",
        message=message,
        details={
            "category": category,
            "version": version,
            **dict(details or {}),
        },
    )
    payload = diagnostic.to_dict()
    payload["category"] = category
    payload["version"] = version
    return payload


class SnapshotStore:
    """In-memory snapshot registry.

    A category keeps exactly one current snapshot. Version increases replace the
    entire content object; content is never merged with the previous value.
    """

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}

    def _current_or_error(
        self, category: str
    ) -> tuple[Snapshot | None, SnapshotOperation | None]:
        current = self._snapshots.get(category)
        if current is None:
            return None, None
        if not current.is_valid():
            return current, SnapshotOperation(
                ok=False,
                code=E_SNAPSHOT_CORRUPT,
                snapshot=current,
                error=_snapshot_error(
                    E_SNAPSHOT_CORRUPT,
                    category,
                    current.version,
                    "stored snapshot checksum does not match its content",
                ),
            )
        return current, None

    def commit(self, snapshot: Snapshot) -> SnapshotOperation:
        if not snapshot.is_valid():
            return SnapshotOperation(
                ok=False,
                code=E_SNAPSHOT_CORRUPT,
                snapshot=snapshot,
                error=_snapshot_error(
                    E_SNAPSHOT_CORRUPT,
                    snapshot.category,
                    snapshot.version,
                    "incoming snapshot checksum does not match its content",
                ),
            )
        current, current_error = self._current_or_error(snapshot.category)
        if current_error is not None:
            return current_error
        if current is not None:
            if snapshot.version < current.version:
                return SnapshotOperation(
                    ok=False,
                    code=E_SNAPSHOT_ROLLBACK,
                    snapshot=current,
                    error=_snapshot_error(
                        E_SNAPSHOT_ROLLBACK,
                        snapshot.category,
                        snapshot.version,
                        "snapshot rollback is forbidden",
                        {
                            "current_version": current.version,
                            "attempted_version": snapshot.version,
                        },
                    ),
                )
            if snapshot.version == current.version:
                if snapshot.checksum == current.checksum:
                    return SnapshotOperation(ok=True, snapshot=current)
                return SnapshotOperation(
                    ok=False,
                    code=E_SNAPSHOT_CORRUPT,
                    snapshot=current,
                    error=_snapshot_error(
                        E_SNAPSHOT_CORRUPT,
                        snapshot.category,
                        snapshot.version,
                        "same version has a different content checksum",
                        {
                            "current_checksum": current.checksum,
                            "incoming_checksum": snapshot.checksum,
                        },
                    ),
                )
        replacement = Snapshot.create(
            snapshot.category, snapshot.version, snapshot.content
        )
        self._snapshots[snapshot.category] = replacement
        return SnapshotOperation(ok=True, snapshot=replacement)

    def publish(
        self,
        category: str,
        content: Any,
        version: int | None = None,
    ) -> SnapshotOperation:
        current, current_error = self._current_or_error(category)
        if current_error is not None:
            return current_error
        effective_version = version
        if effective_version is None:
            effective_version = current.version + 1 if current is not None else 1
        snapshot = Snapshot.create(category, int(effective_version), content)
        return self.commit(snapshot)

    def get(self, category: str) -> SnapshotOperation:
        current, current_error = self._current_or_error(category)
        if current_error is not None:
            return current_error
        if current is None:
            return SnapshotOperation(
                ok=False,
                code=E_SNAPSHOT_UNKNOWN,
                error=_snapshot_error(
                    E_SNAPSHOT_UNKNOWN,
                    category,
                    None,
                    "snapshot category is not present",
                ),
            )
        return SnapshotOperation(ok=True, snapshot=current)

    def query(self, category: str) -> dict[str, Any]:
        operation = self.get(category)
        if not operation.ok or operation.snapshot is None:
            return operation.to_dict(include_content=False)
        snapshot = operation.snapshot
        return {
            "ok": True,
            "code": None,
            "category": snapshot.category,
            "version": snapshot.version,
            "checksum": snapshot.checksum,
            "content": copy.deepcopy(snapshot.content),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "snapshots": {
                category: snapshot.to_dict()
                for category, snapshot in sorted(self._snapshots.items())
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> tuple["SnapshotStore", dict[str, Any] | None]:
        store = cls()
        try:
            snapshots = value.get("snapshots", {})
            if not isinstance(snapshots, dict):
                raise ValueError("snapshots must be an object")
            for category in sorted(snapshots):
                envelope = snapshots[category]
                if not isinstance(envelope, dict):
                    raise ValueError("snapshot envelope must be an object")
                snapshot = Snapshot(
                    category=str(envelope.get("category", category)),
                    version=int(envelope.get("version", 0)),
                    content=copy.deepcopy(envelope.get("content")),
                    checksum=str(envelope.get("checksum", "")),
                )
                operation = store.commit(snapshot)
                if not operation.ok:
                    return store, operation.error
            return store, None
        except Exception as exc:
            category = "<snapshot-file>"
            version = None
            if isinstance(value, Mapping):
                snapshots = value.get("snapshots")
                if isinstance(snapshots, dict) and snapshots:
                    category = sorted(snapshots)[0]
                    envelope = snapshots[category]
                    if isinstance(envelope, Mapping):
                        raw_version = envelope.get("version")
                        version = raw_version if isinstance(raw_version, int) else None
            return store, _snapshot_error(
                E_SNAPSHOT_CORRUPT,
                category,
                version,
                "snapshot file could not be decoded",
                {"type": type(exc).__name__, "message": str(exc)},
            )

    def write(self, path: str | Path) -> None:
        from .util import atomic_write_bytes

        atomic_write_bytes(path, canonical_json_bytes(self.to_dict()) + b"\n")

    @classmethod
    def load(cls, path: str | Path) -> tuple["SnapshotStore", dict[str, Any] | None]:
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("snapshot document must be an object")
            return cls.from_dict(value)
        except Exception as exc:
            return cls(), _snapshot_error(
                E_SNAPSHOT_CORRUPT,
                "<snapshot-file>",
                None,
                "snapshot file is unreadable or malformed",
                {"type": type(exc).__name__, "message": str(exc)},
            )
