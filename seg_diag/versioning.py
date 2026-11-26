"""Versioned whole-snapshot replacement and non-blocking commit gate."""

from __future__ import annotations

import fcntl
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from .codes import CODES
from .errors import DiagnosticError
from .hashing import content_key


T = TypeVar("T")


@dataclass
class VersionedSnapshot(Generic[T]):
    version: int
    checksum: str
    payload: T

    @classmethod
    def create(cls, payload: T, version: int = 1) -> "VersionedSnapshot[T]":
        checksum = payload.get("checksum") if isinstance(payload, dict) else None
        if not checksum:
            checksum = content_key(payload)
        return cls(version=int(version), checksum=str(checksum), payload=payload)

    def whole_replace(self, incoming: "VersionedSnapshot[T]") -> "VersionedSnapshot[T]":
        if incoming.version < self.version:
            raise DiagnosticError(
                CODES.VERSION_CONFLICT,
                "refusing to replace snapshot with a stale version",
                {"current_version": self.version, "incoming_version": incoming.version},
            )
        # A changed checksum at the same declared version is still consumed as
        # a complete replacement. Independent checksum/self-check verification
        # decides whether that replacement is semantically consistent.
        return incoming

    def changed_versus(self, other: "VersionedSnapshot[T] | None") -> bool:
        if other is None:
            return True
        return self.version != other.version or self.checksum != other.checksum


class SnapshotStore(Generic[T]):
    """Keep exactly one current snapshot and replace it only as a whole."""

    def __init__(self) -> None:
        self.current: VersionedSnapshot[T] | None = None

    def install(self, incoming: VersionedSnapshot[T]) -> bool:
        if self.current is None:
            self.current = incoming
            return True
        if not incoming.changed_versus(self.current):
            return False
        self.current = self.current.whole_replace(incoming)
        return True

    def load_json_file(
        self,
        path: str | Path,
        *,
        version_field: str = "ledger_version",
        checksum_field: str = "checksum",
    ) -> tuple[VersionedSnapshot[T], bool]:
        path = Path(path)
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if version_field not in payload or checksum_field not in payload:
            raise DiagnosticError(
                CODES.CONFIG_INVALID,
                "snapshot file lacks version or checksum field",
                {"path": str(path), "version_field": version_field},
            )
        incoming = VersionedSnapshot(
            version=int(payload[version_field]),
            checksum=str(payload[checksum_field]),
            payload=cast(T, payload),
        )
        replaced = self.install(incoming)
        return incoming, replaced


class StageLease:
    def __init__(
        self,
        coordinator: "SnapshotCoordinator",
        token: int,
        snapshot: VersionedSnapshot[Any],
    ) -> None:
        self.coordinator = coordinator
        self.token = token
        self.snapshot = snapshot
        self.released = False

    def commit(self) -> VersionedSnapshot[Any]:
        if self.released:
            raise DiagnosticError(CODES.STAGE_BUSY, "stage lease already released")
        try:
            self.coordinator.active = self.snapshot
            self.coordinator.committed_version = self.snapshot.version
            return self.snapshot
        finally:
            self.discard()

    def discard(self) -> None:
        if self.released:
            return
        self.released = True
        self.coordinator._release(self.token)

    def __enter__(self) -> "StageLease":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if not self.released:
            self.discard()


class SnapshotCoordinator:
    """Allow at most one in-flight configuration snapshot commit."""

    def __init__(self, lock_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._token = 0
        self.active: VersionedSnapshot[Any] | None = None
        self.committed_version = 0
        self._lock_path = Path(lock_path) if lock_path is not None else None
        self._file_handle: Any | None = None

    def begin_submission(self, snapshot: VersionedSnapshot[Any]) -> StageLease:
        if not self._lock.acquire(blocking=False):
            raise DiagnosticError(
                CODES.STAGE_BUSY,
                "another configuration snapshot is being committed",
                {"active_committed_version": self.committed_version},
            )
        if self._lock_path is not None:
            try:
                self._lock_path.parent.mkdir(parents=True, exist_ok=True)
                handle = self._lock_path.open("a+b")
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._file_handle = handle
            except BlockingIOError as exc:
                self._lock.release()
                raise DiagnosticError(
                    CODES.STAGE_BUSY,
                    "another process is committing a configuration snapshot",
                    {"lock_path": str(self._lock_path)},
                ) from exc
            except OSError:
                if self._file_handle is not None:
                    self._file_handle.close()
                    self._file_handle = None
                self._lock.release()
                raise
        self._token += 1
        return StageLease(self, self._token, snapshot)

    def try_begin_submission(self, snapshot: VersionedSnapshot[Any]) -> StageLease | None:
        try:
            return self.begin_submission(snapshot)
        except DiagnosticError as exc:
            if exc.code == CODES.STAGE_BUSY:
                return None
            raise

    def _release(self, token: int) -> None:
        if token != self._token:
            raise DiagnosticError(CODES.INCONSISTENT, "invalid stage lease token")
        try:
            if self._file_handle is not None:
                fcntl.flock(self._file_handle.fileno(), fcntl.LOCK_UN)
                self._file_handle.close()
                self._file_handle = None
        finally:
            self._lock.release()
