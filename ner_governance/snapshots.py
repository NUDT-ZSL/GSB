"""Versioned, checksummed whole-snapshot replacement store."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, fail
from .models import canonical_json, sha256_value

SNAPSHOT_SCHEMA = "ner-governance-snapshot-v1"
ALLOWED_KINDS = {"type_set", "merge_result"}


def _checksum(kind: str, version: int, payload: Any) -> str:
    return sha256_value(
        {
            "schema": SNAPSHOT_SCHEMA,
            "kind": kind,
            "version": version,
            "payload": payload,
        }
    )


@dataclass
class Snapshot:
    kind: str
    version: int
    checksum: str
    payload: Any
    replaces_version: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SNAPSHOT_SCHEMA,
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "replaces_version": self.replaces_version,
            "payload": self.payload,
        }

    def to_bytes(self) -> bytes:
        return (canonical_json(self.to_dict()) + "\n").encode("utf-8")

    @classmethod
    def from_dict(cls, data: Any) -> "Snapshot":
        if not isinstance(data, dict):
            fail(
                E_SNAPSHOT_CORRUPT,
                "snapshot must be a JSON object",
                field="$",
                reason="invalid_root_type",
            )
        required = {"schema", "kind", "version", "checksum", "payload"}
        missing = sorted(required - set(data))
        if missing:
            fail(
                E_SNAPSHOT_CORRUPT,
                f"snapshot is missing {missing[0]}",
                field=missing[0],
                reason="required_field_missing",
            )
        allowed = required | {"replaces_version"}
        unknown = sorted(set(data) - allowed)
        if unknown:
            fail(
                E_SNAPSHOT_CORRUPT,
                f"snapshot contains unknown field {unknown[0]!r}",
                field=unknown[0],
                reason="unknown_field",
            )
        if data["schema"] != SNAPSHOT_SCHEMA:
            fail(
                E_SNAPSHOT_CORRUPT,
                "unsupported snapshot schema",
                field="schema",
                reason="schema_mismatch",
                actual=data["schema"],
            )
        kind = data["kind"]
        if kind not in ALLOWED_KINDS:
            fail(
                E_SNAPSHOT_CORRUPT,
                f"unknown snapshot kind {kind!r}",
                field="kind",
                reason="unknown_kind",
                value=kind,
            )
        version = data["version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            fail(
                E_SNAPSHOT_CORRUPT,
                "snapshot version must be a positive integer",
                field="version",
                reason="invalid_version",
            )
        expected_checksum = _checksum(kind, version, data["payload"])
        if data["checksum"] != expected_checksum:
            fail(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum does not match its content",
                field="checksum",
                reason="checksum_mismatch",
                expected=expected_checksum,
                actual=data["checksum"],
            )
        replaces = data.get("replaces_version")
        if replaces is not None and (
            isinstance(replaces, bool)
            or not isinstance(replaces, int)
            or replaces < 1
        ):
            fail(
                E_SNAPSHOT_CORRUPT,
                "replaces_version must be a positive integer or null",
                field="replaces_version",
                reason="invalid_version",
            )
        return cls(
            kind=kind,
            version=version,
            checksum=data["checksum"],
            payload=data["payload"],
            replaces_version=replaces,
        )


class SnapshotStore:
    """One current snapshot per kind, always replaced as a whole file."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def path_for(self, kind: str) -> Path:
        if kind not in ALLOWED_KINDS:
            fail(
                E_SNAPSHOT_CORRUPT,
                f"unknown snapshot kind {kind!r}",
                field="kind",
                reason="unknown_kind",
                value=kind,
            )
        return self.root / f"{kind}.snapshot.json"

    def load(self, kind: str) -> Snapshot:
        path = self.path_for(kind)
        if not path.exists():
            fail(
                E_SNAPSHOT_CORRUPT,
                f"snapshot {kind!r} does not exist",
                field="path",
                reason="snapshot_missing",
                path=str(path),
            )
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            fail(
                E_SNAPSHOT_CORRUPT,
                f"cannot read snapshot: {exc}",
                field="path",
                reason="invalid_json",
                path=str(path),
            )
        snapshot = Snapshot.from_dict(data)
        if snapshot.kind != kind:
            fail(
                E_SNAPSHOT_CORRUPT,
                "snapshot kind does not match its requested slot",
                field="kind",
                reason="slot_kind_mismatch",
                expected=kind,
                actual=snapshot.kind,
            )
        return snapshot

    def write(
        self,
        kind: str,
        payload: Any,
        *,
        base_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> Snapshot:
        """Write a complete new snapshot, never patching an old payload."""

        path = self.path_for(kind)
        current: Snapshot | None = None
        if path.exists():
            current = self.load(kind)
        if current is not None:
            if base_version is not None:
                if base_version < current.version:
                    fail(
                        E_SNAPSHOT_ROLLBACK,
                        "base_version would roll back the current snapshot",
                        field="base_version",
                        reason="version_regression",
                        current_version=current.version,
                        requested_base_version=base_version,
                    )
                if base_version > current.version:
                    fail(
                        E_SNAPSHOT_CORRUPT,
                        "base_version is ahead of the current snapshot",
                        field="base_version",
                        reason="version_ahead",
                        current_version=current.version,
                        requested_base_version=base_version,
                    )
            if (
                expected_checksum is not None
                and expected_checksum != current.checksum
            ):
                fail(
                    E_SNAPSHOT_CORRUPT,
                    "expected_checksum does not match the current snapshot",
                    field="expected_checksum",
                    reason="checksum_mismatch",
                    expected=expected_checksum,
                    actual=current.checksum,
                )
            version = current.version + 1
            replaces_version = current.version
        else:
            version = 1
            replaces_version = None
        snapshot = Snapshot(
            kind=kind,
            version=version,
            checksum=_checksum(kind, version, payload),
            payload=payload,
            replaces_version=replaces_version,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = self.root / f".{kind}.snapshot.json.tmp"
        try:
            with temp_path.open("wb") as handle:
                handle.write(snapshot.to_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            fail(
                E_SNAPSHOT_CORRUPT,
                f"cannot write snapshot: {exc}",
                field="path",
                reason="write_error",
                path=str(path),
            )
        return snapshot

    def restore(self, snapshot: Snapshot | dict[str, Any]) -> Snapshot:
        """Whole-file restore; an older version is always rejected."""

        candidate = (
            snapshot
            if isinstance(snapshot, Snapshot)
            else Snapshot.from_dict(snapshot)
        )
        expected_checksum = _checksum(
            candidate.kind,
            candidate.version,
            candidate.payload,
        )
        if candidate.checksum != expected_checksum:
            fail(
                E_SNAPSHOT_CORRUPT,
                "candidate snapshot checksum does not match its content",
                field="checksum",
                reason="checksum_mismatch",
                expected=expected_checksum,
                actual=candidate.checksum,
            )
        current = self.load(candidate.kind)
        if candidate.version <= current.version:
            fail(
                E_SNAPSHOT_ROLLBACK,
                "snapshot restore would move to an older or equal version",
                field="version",
                reason="snapshot_rollback",
                current_version=current.version,
                requested_version=candidate.version,
                kind=candidate.kind,
            )
        path = self.path_for(candidate.kind)
        self.root.mkdir(parents=True, exist_ok=True)
        temp_path = self.root / f".{candidate.kind}.restore.json.tmp"
        try:
            with temp_path.open("wb") as handle:
                handle.write(candidate.to_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        except OSError as exc:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            fail(
                E_SNAPSHOT_CORRUPT,
                f"cannot restore snapshot: {exc}",
                field="path",
                reason="write_error",
                path=str(path),
            )
        return candidate
