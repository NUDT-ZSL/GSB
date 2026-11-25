"""Versioned snapshots, checksums, rollback rejection, and commit locking."""
from __future__ import annotations

import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_bytes, hash_json
from .errors import E_SNAPSHOT_CHECKSUM, E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY, WorkbenchError


class SnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.Lock()
        self._lock_handle = None
        self._lock_file = self.root / "config_commit.lock"

    def path_for(self, kind: str) -> Path:
        return self.root / f"{kind}_snapshot.json"

    def checksum_for(self, version: int, data: Any) -> str:
        return hash_json({"version": version, "data": data})

    @contextmanager
    def config_commit_guard(self) -> Iterator[None]:
        if not self._thread_lock.acquire(blocking=False):
            raise WorkbenchError(E_STAGE_BUSY, "Another configuration snapshot commit is already active", field="commit_lock")

        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        handle = self._lock_file.open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkbenchError(
                    E_STAGE_BUSY,
                    "Configuration snapshot is locked by another process",
                    field="commit_lock",
                ) from exc
            self._lock_handle = handle
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
                self._lock_handle = None
                self._thread_lock.release()

    def _write_atomic(self, path: Path, envelope: dict[str, Any]) -> None:
        import os
        import tempfile

        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(canonical_bytes(envelope))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def current_version(self, kind: str) -> int | None:
        path = self.path_for(kind)
        if not path.exists():
            return None
        return int(self.load(kind)["version"])

    def load(self, kind: str) -> dict[str, Any]:
        path = self.path_for(kind)
        if not path.exists():
            raise WorkbenchError("E_NOT_FOUND", f"Snapshot does not exist: {kind}", field=kind)
        import json

        envelope = json.loads(path.read_text(encoding="utf-8"))
        if envelope.get("kind") != kind:
            raise WorkbenchError(E_SNAPSHOT_CHECKSUM, "Snapshot kind mismatch", field=kind)
        version = envelope.get("version")
        data = envelope.get("data")
        checksum = envelope.get("checksum")
        if not isinstance(version, int) or version < 1:
            raise WorkbenchError(E_SNAPSHOT_CHECKSUM, "Snapshot version is invalid", field=f"{kind}.version")
        expected = self.checksum_for(version, data)
        if checksum != expected:
            raise WorkbenchError(
                E_SNAPSHOT_CHECKSUM,
                "Snapshot checksum does not match its versioned content",
                field=f"{kind}.checksum",
                details={"version": version, "expected": expected, "provided": checksum},
            )
        return envelope

    def commit(self, kind: str, data: Any) -> dict[str, Any]:
        if kind == "config":
            with self.config_commit_guard():
                return self._commit_unlocked(kind, data)
        return self._commit_unlocked(kind, data)

    def _commit_unlocked(self, kind: str, data: Any) -> dict[str, Any]:
        current = None
        path = self.path_for(kind)
        if path.exists():
            current = self.load(kind)
        next_version = 1 if current is None else int(current["version"]) + 1
        envelope = {
            "kind": kind,
            "version": next_version,
            "checksum": self.checksum_for(next_version, data),
            "data": data,
        }
        self._write_atomic(path, envelope)
        return envelope

    def install_version(self, kind: str, version: int, data: Any, *, checksum: str | None = None) -> dict[str, Any]:
        """Install an explicit newer snapshot; older versions are rollback attempts."""
        current_version = self.current_version(kind)
        if current_version is not None and version <= current_version:
            raise WorkbenchError(
                E_SNAPSHOT_ROLLBACK,
                "Refusing to replace a newer snapshot with an older or equal version",
                field=f"{kind}.version",
                details={"requested_version": version, "current_version": current_version},
            )
        expected_checksum = self.checksum_for(version, data)
        if checksum is not None and checksum != expected_checksum:
            raise WorkbenchError(
                E_SNAPSHOT_CHECKSUM,
                "Refusing snapshot with a stale or incorrect content checksum",
                field=f"{kind}.checksum",
                details={"expected": expected_checksum, "provided": checksum},
            )
        envelope = {"kind": kind, "version": version, "checksum": expected_checksum, "data": data}
        self._write_atomic(self.path_for(kind), envelope)
        return envelope
