"""Whole-snapshot versioning with monotonic rollback protection."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from .cache import CommitGate, _atomic_write
from .errors import SnapshotCorrupt, SnapshotRollback
from .serialization import canonical_bytes, content_checksum

SNAPSHOT_FORMAT = "sceneopt-snapshot-v1"


@dataclass(frozen=True)
class Snapshot:
    payload: dict[str, Any]
    checksum: str

    @property
    def version(self) -> int:
        return int(self.payload["version"])

    @property
    def config_digest(self) -> str:
        return str(self.payload["config_digest"])

    @property
    def scenes(self) -> dict[str, Any]:
        return self.payload["scenes"]

    @classmethod
    def create(
        cls,
        *,
        version: int,
        config_digest: str,
        scenes: dict[str, Any],
        summary: dict[str, Any] | None = None,
    ) -> "Snapshot":
        payload: dict[str, Any] = {
            "format": SNAPSHOT_FORMAT,
            "version": int(version),
            "config_digest": str(config_digest),
            "scenes": scenes,
        }
        if summary is not None:
            payload["summary"] = summary
        checksum = content_checksum(payload)
        snapshot = cls(payload=payload, checksum=checksum)
        snapshot.validate()
        return snapshot

    def validate(self) -> None:
        if not isinstance(self.payload, dict):
            raise SnapshotCorrupt("payload is not an object")
        if self.payload.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotCorrupt("unknown snapshot format")
        version = self.payload.get("version")
        if not isinstance(version, int) or version < 1:
            raise SnapshotCorrupt("snapshot version must be an integer >= 1")
        if not isinstance(self.payload.get("config_digest"), str):
            raise SnapshotCorrupt("config_digest is missing")
        if not isinstance(self.payload.get("scenes"), dict):
            raise SnapshotCorrupt("scenes must be an object")
        actual = content_checksum(self.payload)
        if actual != self.checksum:
            raise SnapshotCorrupt(
                "content checksum mismatch",
                expected_checksum=self.checksum,
                actual_checksum=actual,
            )

    def to_document(self) -> dict[str, Any]:
        return {
            "format": SNAPSHOT_FORMAT,
            "checksum": self.checksum,
            "payload": self.payload,
        }

    @classmethod
    def from_document(cls, document: Any) -> "Snapshot":
        if not isinstance(document, dict):
            raise SnapshotCorrupt("snapshot document is not an object")
        if document.get("format") != SNAPSHOT_FORMAT:
            raise SnapshotCorrupt("snapshot document format is invalid")
        payload = document.get("payload")
        checksum = document.get("checksum")
        if not isinstance(payload, dict) or not isinstance(checksum, str):
            raise SnapshotCorrupt("snapshot payload or checksum is missing")
        snapshot = cls(payload=payload, checksum=checksum)
        snapshot.validate()
        return snapshot


@dataclass(frozen=True)
class SnapshotCommitResult:
    committed: bool
    replaced: bool
    version: int
    previous_version: int | None
    checksum_changed: bool
    path: str


class SnapshotStore:
    def __init__(self, path: str, *, lock_path: str | None = None) -> None:
        self.path = os.path.abspath(path)
        self.directory = os.path.dirname(self.path)
        os.makedirs(self.directory, exist_ok=True)
        self.lock_path = (
            os.path.abspath(lock_path)
            if lock_path is not None
            else os.path.join(self.directory, ".commit.lock")
        )
        self.gate = CommitGate(self.lock_path)

    def load(self) -> Snapshot | None:
        try:
            with open(self.path, "rb") as handle:
                raw = handle.read()
        except FileNotFoundError:
            return None
        try:
            document = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise SnapshotCorrupt(f"invalid JSON: {exc}") from exc
        return Snapshot.from_document(document)

    def version_path(self, version: int) -> str:
        return os.path.join(self.directory, f"snapshot-v{int(version):08d}.json")

    def _write(self, snapshot: Snapshot) -> None:
        document_bytes = canonical_bytes(snapshot.to_document())
        version_path = self.version_path(snapshot.version)
        if os.path.isfile(version_path):
            with open(version_path, "rb") as handle:
                existing = handle.read()
            if existing != document_bytes:
                raise SnapshotRollback(
                    snapshot.version,
                    snapshot.version,
                    reason="version file already exists with different content",
                )
        else:
            _atomic_write(version_path, document_bytes)
        _atomic_write(self.path, document_bytes)

    def commit(
        self,
        snapshot: Snapshot,
        *,
        gate: CommitGate | None = None,
        gate_held: bool = False,
    ) -> SnapshotCommitResult:
        snapshot.validate()
        acquired = False
        active_gate = gate
        if not gate_held:
            active_gate = gate or self.gate
            active_gate.acquire("snapshot_commit")
            acquired = True
        try:
            current = self.load()
            if current is not None:
                if snapshot.version < current.version:
                    raise SnapshotRollback(snapshot.version, current.version)
                if (
                    snapshot.version == current.version
                    and snapshot.checksum != current.checksum
                ):
                    raise SnapshotRollback(snapshot.version, current.version)
                if (
                    snapshot.version == current.version
                    and snapshot.checksum == current.checksum
                ):
                    return SnapshotCommitResult(
                        committed=False,
                        replaced=False,
                        version=snapshot.version,
                        previous_version=current.version,
                        checksum_changed=False,
                        path=self.path,
                    )
            previous_version = None if current is None else current.version
            checksum_changed = current is None or current.checksum != snapshot.checksum
            self._write(snapshot)
            return SnapshotCommitResult(
                committed=True,
                replaced=current is not None,
                version=snapshot.version,
                previous_version=previous_version,
                checksum_changed=checksum_changed,
                path=self.path,
            )
        finally:
            if acquired and active_gate is not None:
                active_gate.release()

