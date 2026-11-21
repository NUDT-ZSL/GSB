"""Versioned snapshots with atomic whole-sale replacement.

A snapshot bundles the query-index bytes and the metrics bytes under a
monotonically increasing version number and a content checksum.  Any
change in version or checksum triggers a *full* replacement, never an
incremental patch.  Publication is atomic: readers either see the whole
old snapshot or the whole new one, never a mixture.  Corrupt payloads or
checksum mismatches raise E_SNAPSHOT_CORRUPT.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import threading

from .errors import E_SNAPSHOT_CORRUPT, PanoError


@dataclasses.dataclass(frozen=True)
class Snapshot:
    version: int
    checksum: str
    index_bytes: bytes
    metrics_bytes: bytes
    meta: dict

    @staticmethod
    def compute_checksum(index_bytes: bytes, metrics_bytes: bytes, meta: dict) -> str:
        head = json.dumps(meta, sort_keys=True).encode("utf-8")
        return hashlib.sha256(head + b"|" + index_bytes + b"|" + metrics_bytes).hexdigest()

    @classmethod
    def build(cls, version: int, index_bytes: bytes, metrics_bytes: bytes,
              meta: dict) -> "Snapshot":
        return cls(
            version=version,
            checksum=cls.compute_checksum(index_bytes, metrics_bytes, meta),
            index_bytes=index_bytes, metrics_bytes=metrics_bytes, meta=meta,
        )

    def verify(self) -> None:
        expect = self.compute_checksum(self.index_bytes, self.metrics_bytes, self.meta)
        if expect != self.checksum:
            raise PanoError(
                E_SNAPSHOT_CORRUPT, "snapshot checksum mismatch",
                version=self.version, expected=expect, actual=self.checksum,
            )


class SnapshotStore:
    """Filesystem persistence: one file per version + an atomic CURRENT."""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(self.dir, exist_ok=True)
        self._pointer = os.path.join(self.dir, "CURRENT")
        self._lock = threading.Lock()

    def _path(self, version: int) -> str:
        return os.path.join(self.dir, f"snapshot_v{version}.json")

    def latest_version(self) -> int:
        if not os.path.exists(self._pointer):
            return 0
        with open(self._pointer, "r", encoding="utf-8") as fh:
            return int(fh.read().strip())

    def commit(self, index_bytes: bytes, metrics_bytes: bytes, meta: dict) -> Snapshot:
        with self._lock:
            version = self.latest_version() + 1
            snap = Snapshot.build(version, index_bytes, metrics_bytes,
                                  {**meta, "version": version})
            payload = {
                "version": snap.version,
                "checksum": snap.checksum,
                "meta": snap.meta,
                "index_hex": snap.index_bytes.hex(),
                "metrics_hex": snap.metrics_bytes.hex(),
            }
            tmp = self._path(version) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, sort_keys=True)
            os.replace(tmp, self._path(version))          # atomic file publish
            tmp_ptr = self._pointer + ".tmp"
            with open(tmp_ptr, "w", encoding="utf-8") as fh:
                fh.write(str(version))
            os.replace(tmp_ptr, self._pointer)            # atomic pointer swap
            return snap

    def load(self, version: int) -> Snapshot:
        path = self._path(version)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            snap = Snapshot(
                version=int(payload["version"]),
                checksum=str(payload["checksum"]),
                index_bytes=bytes.fromhex(payload["index_hex"]),
                metrics_bytes=bytes.fromhex(payload["metrics_hex"]),
                meta=dict(payload["meta"]),
            )
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise PanoError(
                E_SNAPSHOT_CORRUPT, "snapshot file unreadable",
                version=version, path=path, error=str(exc),
            ) from exc
        snap.verify()
        return snap

    def load_current(self) -> Snapshot | None:
        version = self.latest_version()
        if version == 0:
            return None
        return self.load(version)


class SnapshotManager:
    """In-memory atomic holder: the query side only ever reads ``current``."""

    def __init__(self):
        self._lock = threading.Lock()
        self._current: Snapshot | None = None

    def publish(self, snap: Snapshot) -> Snapshot:
        snap.verify()
        with self._lock:
            if self._current is not None and snap.version <= self._current.version:
                raise PanoError(
                    E_SNAPSHOT_CORRUPT, "non-monotonic snapshot version",
                    current=self._current.version, incoming=snap.version,
                )
            self._current = snap          # single-reference atomic swap
            return snap

    def current(self) -> Snapshot | None:
        return self._current              # readers never see partial state
