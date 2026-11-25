"""Versioned whole-snapshot replacement with non-blocking commit control."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - the execution target is Linux, fallback is defensive.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

from .codec import canonical_bytes, canonical_hash
from .errors import (
    E_STAGE_BUSY,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    MoeError,
)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def snapshot_checksum(body: dict[str, Any]) -> str:
    return canonical_hash(body)


def verify_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    required = {"version", "policy", "experts", "schedule", "checksum"}
    missing = sorted(required - set(snapshot))
    if missing:
        raise MoeError(
            code=E_SNAPSHOT_CORRUPT,
            message="snapshot is missing required fields",
            details={"missing_fields": missing},
        )
    body = {
        "version": snapshot["version"],
        "policy": snapshot["policy"],
        "experts": snapshot["experts"],
        "schedule": snapshot["schedule"],
    }
    actual = snapshot_checksum(body)
    if actual != snapshot["checksum"]:
        raise MoeError(
            code=E_SNAPSHOT_CORRUPT,
            message="snapshot checksum verification failed",
            details={
                "version": snapshot["version"],
                "expected_checksum": snapshot["checksum"],
                "actual_checksum": actual,
            },
        )
    return snapshot


class SnapshotStore:
    """Stores one active immutable snapshot and an append-only version archive."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"
        self.history_dir = self.root / "history"
        self.history_dir.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".commit.lock"
        self._thread_lock = threading.Lock()

    def load_current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        try:
            snapshot = json.loads(self.current_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MoeError(
                code=E_SNAPSHOT_CORRUPT,
                message="current snapshot is not valid JSON",
            ) from exc
        if not isinstance(snapshot, dict):
            raise MoeError(
                code=E_SNAPSHOT_CORRUPT,
                message="current snapshot must be a JSON object",
            )
        return verify_snapshot(snapshot)

    def current_version(self) -> int:
        current = self.load_current()
        return 0 if current is None else int(current["version"])

    def _acquire_guard(self) -> tuple[Any, Any]:
        if not self._thread_lock.acquire(blocking=False):
            raise MoeError(
                code=E_STAGE_BUSY,
                message="another snapshot commit is already active",
                field="snapshot_commit",
            )
        lock_handle = None
        try:
            if fcntl is not None:
                lock_handle = self.lock_path.open("a+b")
                try:
                    fcntl.flock(
                        lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                    )
                except BlockingIOError as exc:
                    lock_handle.close()
                    lock_handle = None
                    raise MoeError(
                        code=E_STAGE_BUSY,
                        message="another process is committing a snapshot",
                        field="snapshot_commit",
                    ) from exc
            return self._thread_lock, lock_handle
        except BaseException:
            self._thread_lock.release()
            raise

    def _release_guard(self, thread_lock: Any, lock_handle: Any) -> None:
        try:
            if lock_handle is not None:
                if fcntl is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
        finally:
            thread_lock.release()

    @contextmanager
    def hold_commit_guard(self) -> Iterator[None]:
        """Expose the non-blocking commit gate for integration readiness checks."""

        thread_lock, lock_handle = self._acquire_guard()
        try:
            yield
        finally:
            self._release_guard(thread_lock, lock_handle)

    def commit(
        self,
        *,
        version: int,
        policy: dict[str, Any],
        experts: dict[str, Any],
        schedule: dict[str, Any],
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise MoeError(
                code=E_SNAPSHOT_ROLLBACK,
                message="snapshot version must be a positive integer",
                field="version",
                details={"version": version},
            )
        body = {
            "version": version,
            "policy": policy,
            "experts": experts,
            "schedule": schedule,
        }
        snapshot = {**body, "checksum": snapshot_checksum(body)}
        thread_lock, lock_handle = self._acquire_guard()
        try:
            current = self.load_current()
            current_version = 0 if current is None else int(current["version"])
            if expected_version is not None and current_version != expected_version:
                raise MoeError(
                    code=E_STAGE_BUSY,
                    message="snapshot changed since the caller read its version",
                    field="expected_version",
                    details={
                        "expected_version": expected_version,
                        "current_version": current_version,
                    },
                )
            if current is not None and version < current_version:
                raise MoeError(
                    code=E_SNAPSHOT_ROLLBACK,
                    message="an older snapshot may not overwrite a newer snapshot",
                    field="version",
                    details={
                        "requested_version": version,
                        "current_version": current_version,
                    },
                )
            if current is not None and version == current_version:
                if snapshot["checksum"] == current["checksum"]:
                    return current
                raise MoeError(
                    code=E_STAGE_BUSY,
                    message="same snapshot version was already committed with different content",
                    field="version",
                    details={
                        "version": version,
                        "current_checksum": current["checksum"],
                        "requested_checksum": snapshot["checksum"],
                    },
                )

            payload = canonical_bytes(snapshot)
            history_path = self.history_dir / f"snapshot-{version:020d}.json"
            _atomic_write(history_path, payload)
            _atomic_write(self.current_path, payload)
            return snapshot
        finally:
            self._release_guard(thread_lock, lock_handle)

    def load_history(self, version: int) -> dict[str, Any]:
        path = self.history_dir / f"snapshot-{version:020d}.json"
        if not path.exists():
            raise MoeError(
                code=E_SNAPSHOT_CORRUPT,
                message="snapshot history version does not exist",
                details={"version": version},
            )
        try:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MoeError(
                code=E_SNAPSHOT_CORRUPT,
                message="snapshot history is not valid JSON",
                details={"version": version},
            ) from exc
        return verify_snapshot(snapshot)
