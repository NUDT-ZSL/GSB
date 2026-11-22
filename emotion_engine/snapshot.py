"""Versioned whole-snapshot replacement and integrity checks."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Mapping

from .errors import E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, EngineError
from .util import sha256_json


@dataclass(frozen=True)
class Snapshot:
    kind: str
    version: int
    payload: Any
    checksum: str

    @classmethod
    def create(cls, kind: str, version: int, payload: Any) -> "Snapshot":
        frozen_payload = copy.deepcopy(payload)
        return cls(
            kind=kind,
            version=int(version),
            payload=frozen_payload,
            checksum=sha256_json(frozen_payload),
        )

    def verify(self) -> None:
        actual = sha256_json(self.payload)
        if actual != self.checksum:
            raise EngineError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum does not match its content",
                kind=self.kind,
                version=self.version,
                expected_checksum=self.checksum,
                actual_checksum=actual,
            )

    def metadata(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
        }


class SnapshotStore:
    """A store that only permits monotonic whole-object replacement."""

    def __init__(self) -> None:
        self._snapshots: dict[str, Snapshot] = {}
        self.replacement_count = 0

    def replace(self, snapshot: Snapshot) -> dict[str, Any]:
        snapshot.verify()
        current = self._snapshots.get(snapshot.kind)
        if current is not None and snapshot.version <= current.version:
            raise EngineError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot replacement must advance monotonically",
                kind=snapshot.kind,
                current_version=current.version,
                attempted_version=snapshot.version,
            )
        self._snapshots[snapshot.kind] = snapshot
        self.replacement_count += 1
        return {
            "kind": snapshot.kind,
            "previous_version": current.version if current else None,
            "version": snapshot.version,
            "checksum": snapshot.checksum,
            "strategy": "whole_snapshot_replace",
        }

    def get(self, kind: str) -> Snapshot | None:
        snapshot = self._snapshots.get(kind)
        if snapshot is not None:
            snapshot.verify()
        return snapshot

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for kind in sorted(self._snapshots):
            snapshot = self._snapshots[kind]
            snapshot.verify()
            result[kind] = snapshot.metadata()
        return result


def snapshot_view_payloads(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "frames": view["frames"],
        "segments": view["segments"],
        "windows": [row["window"] for row in view["frames"]],
    }


def commit_view(store: SnapshotStore, view: Mapping[str, Any], version: int) -> list[dict[str, Any]]:
    payloads = snapshot_view_payloads(view)
    replacements: list[dict[str, Any]] = []
    for kind in ("frames", "segments", "windows"):
        snapshot = Snapshot.create(kind, version, payloads[kind])
        replacements.append(store.replace(snapshot))
    return replacements


def verify_snapshot_store(store: SnapshotStore) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for kind in ("frames", "segments", "windows"):
        snapshot = store.get(kind)
        checks.append(
            {
                "name": f"snapshot_{kind}",
                "ok": snapshot is not None,
                "metadata": snapshot.metadata() if snapshot else None,
            }
        )
    return checks
