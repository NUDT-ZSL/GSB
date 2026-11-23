"""Whole-object, monotonic snapshots with content checksums."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import BusinessError, ErrorCode
from .hashing import array_content_hash, sha256_json
from .mask import MaskState

SNAPSHOT_KINDS = ("mask", "style", "image")


def snapshot_checksum(kind: str, payload: Any) -> str:
    if kind == "mask":
        if not isinstance(payload, MaskState):
            raise TypeError("mask snapshot payload must be a MaskState")
        return sha256_json({"kind": "mask", "checksum": payload.checksum})
    if kind == "style":
        return sha256_json({"kind": "style", "payload": payload})
    if kind == "image":
        image = np.asarray(payload)
        return sha256_json(
            {
                "kind": "image",
                "shape": [int(v) for v in image.shape],
                "array_hash": array_content_hash(image),
            }
        )
    raise BusinessError(
        ErrorCode.SNAPSHOT_CORRUPT,
        "unknown snapshot kind",
        kind=kind,
        allowed=list(SNAPSHOT_KINDS),
    )


def _clone_payload(payload: Any) -> Any:
    if isinstance(payload, np.ndarray):
        return payload.copy()
    if isinstance(payload, MaskState):
        return MaskState(tuple(type(region)(region.region_id, region.mask.copy()) for region in payload.regions))
    return copy.deepcopy(payload)


@dataclass(frozen=True)
class Snapshot:
    kind: str
    version: int
    checksum: str
    payload: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
        }


class SnapshotStore:
    """Snapshots are replaced as complete immutable values, never patched."""

    def __init__(self) -> None:
        self._current: dict[str, Snapshot] = {}

    def current(self, kind: str) -> Snapshot | None:
        return self._current.get(kind)

    def current_version(self, kind: str) -> int:
        snapshot = self.current(kind)
        return snapshot.version if snapshot else 0

    def next_version(self, kind: str, payload: Any) -> int:
        checksum = snapshot_checksum(kind, payload)
        current = self.current(kind)
        if current is None:
            return 1
        if current.checksum == checksum:
            return current.version
        return current.version + 1

    def commit(
        self,
        kind: str,
        version: int,
        payload: Any,
        expected_checksum: str | None = None,
    ) -> Snapshot:
        if kind not in SNAPSHOT_KINDS:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "unknown snapshot kind",
                kind=kind,
                allowed=list(SNAPSHOT_KINDS),
            )
        if isinstance(version, (bool, np.bool_)) or not isinstance(version, (int, np.integer)):
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot version must be an integer",
                kind=kind,
                version=version,
            )
        version = int(version)
        if version <= 0:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot version must be positive",
                kind=kind,
                version=version,
            )

        checksum = snapshot_checksum(kind, payload)
        if expected_checksum is not None and expected_checksum != checksum:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot checksum does not match payload",
                kind=kind,
                expected=expected_checksum,
                actual=checksum,
            )

        current = self.current(kind)
        if current is not None:
            if version < current.version:
                raise BusinessError(
                    ErrorCode.SNAPSHOT_ROLLBACK,
                    "an older snapshot version cannot overwrite a newer one",
                    kind=kind,
                    attempted_version=version,
                    current_version=current.version,
                )
            if version == current.version and checksum != current.checksum:
                raise BusinessError(
                    ErrorCode.SNAPSHOT_CORRUPT,
                    "one snapshot version cannot identify different content",
                    kind=kind,
                    version=version,
                    current_checksum=current.checksum,
                    actual=checksum,
                )

        snapshot = Snapshot(
            kind=kind,
            version=version,
            checksum=checksum,
            payload=_clone_payload(payload),
        )
        self._current[kind] = snapshot
        return snapshot

    def get(self, kind: str) -> Any:
        snapshot = self.current(kind)
        if snapshot is None:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot does not exist",
                kind=kind,
            )
        return _clone_payload(snapshot.payload)

    def verify(self, kind: str, expected_checksum: str | None = None) -> Snapshot:
        snapshot = self.current(kind)
        if snapshot is None:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot does not exist",
                kind=kind,
            )
        actual = snapshot_checksum(kind, snapshot.payload)
        if actual != snapshot.checksum:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot payload no longer matches its stored checksum",
                kind=kind,
                version=snapshot.version,
                expected=snapshot.checksum,
                actual=actual,
            )
        if expected_checksum is not None and expected_checksum != actual:
            raise BusinessError(
                ErrorCode.SNAPSHOT_CORRUPT,
                "snapshot checksum differs from the expected checksum",
                kind=kind,
                version=snapshot.version,
                expected=expected_checksum,
                actual=actual,
            )
        return snapshot

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for kind in SNAPSHOT_KINDS:
            snapshot = self.current(kind)
            result[kind] = (
                None if snapshot is None else snapshot.to_dict()
            )
        return result
