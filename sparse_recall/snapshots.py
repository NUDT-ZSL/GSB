"""Atomic, checksummed, non-blocking versioned snapshot replacement."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    read_json,
    sha256_value,
    write_json_atomic,
)
from .errors import (
    E_CONFIG,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    RecallError,
)


SNAPSHOT_SCHEMA = "sparse-recall.snapshot.v1"
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _THREAD_LOCKS[key] = lock
        return lock


def _try_os_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        if handle.read(1) == b"":
            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


@contextmanager
def nonblocking_commit_lock(lock_path: str | Path) -> Iterator[None]:
    """Acquire immediately or raise E_STAGE_BUSY; never waits."""

    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock_for(path)
    if not thread_lock.acquire(blocking=False):
        raise RecallError(E_STAGE_BUSY, "commit", "another batch plan is committing")
    handle: Any = None
    try:
        handle = open(path, "a+b")
        try:
            _try_os_lock(handle)
        except (OSError, BlockingIOError) as exc:
            raise RecallError(
                E_STAGE_BUSY,
                "commit",
                "another process is committing a batch plan",
            ) from exc
        yield
    finally:
        if handle is not None:
            _release_os_lock(handle)
            handle.close()
        thread_lock.release()


class VersionedSnapshotStore:
    """Stores exactly one latest snapshot and replaces it as a whole."""

    def __init__(self, path: str | Path, *, snapshot_kind: str) -> None:
        if not snapshot_kind:
            raise RecallError(E_CONFIG, "snapshot", "snapshot_kind must not be empty")
        self.path = Path(path)
        self.snapshot_kind = snapshot_kind
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def load(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        try:
            raw = read_json(self.path)
        except FileNotFoundError:
            return None
        except RecallError as exc:
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                f"cannot parse snapshot {self.path}",
            ) from exc
        if not isinstance(raw, dict):
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                "snapshot root must be an object",
            )
        checksum = raw.get("content_sha256")
        if not isinstance(checksum, str) or not checksum:
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                "snapshot checksum is missing",
            )
        body = {key: value for key, value in raw.items() if key != "content_sha256"}
        actual = sha256_value(body)
        if actual != checksum:
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                "snapshot checksum mismatch",
                details={"expected": checksum, "actual": actual},
            )
        if body.get("schema") != SNAPSHOT_SCHEMA:
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                "unsupported snapshot schema",
            )
        if body.get("snapshot_kind") != self.snapshot_kind:
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                "snapshot kind mismatch",
            )
        version = body.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RecallError(
                E_SNAPSHOT_CORRUPT,
                "snapshot",
                "snapshot version must be a positive integer",
            )
        return body

    def commit(
        self,
        *,
        version: int,
        stage: str,
        payload: dict[str, Any],
        completed_batches: dict[str, list[str]] | None = None,
        failed_batches: dict[str, list[str]] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise RecallError(E_CONFIG, "snapshot", "version must be >= 1")
        with nonblocking_commit_lock(self.lock_path):
            current = self.load()
            if current is not None:
                current_version = int(current["version"])
                incoming_content = sha256_value(
                    {
                        "stage": stage,
                        "payload": payload,
                        "completed_batches": completed_batches or {},
                        "failed_batches": failed_batches or {},
                        "metadata": metadata or {},
                    }
                )
                current_content = sha256_value(
                    {
                        "stage": current.get("stage"),
                        "payload": current.get("payload"),
                        "completed_batches": current.get("completed_batches"),
                        "failed_batches": current.get("failed_batches"),
                        "metadata": current.get("metadata"),
                    }
                )
                if version < current_version:
                    raise RecallError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot",
                        f"cannot replace version {current_version} with {version}",
                        details={
                            "current_version": current_version,
                            "incoming_version": version,
                        },
                    )
                if version == current_version:
                    if incoming_content == current_content:
                        return current
                    raise RecallError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot",
                        "same version cannot replace different snapshot content",
                        details={
                            "current_version": current_version,
                            "incoming_version": version,
                        },
                    )
            body = {
                "schema": SNAPSHOT_SCHEMA,
                "snapshot_kind": self.snapshot_kind,
                "version": version,
                "stage": stage,
                "completed_batches": completed_batches or {},
                "failed_batches": failed_batches or {},
                "payload": payload,
                "metadata": metadata or {},
            }
            checksum = sha256_value(body)
            body["content_sha256"] = checksum
            write_json_atomic(self.path, body)
            return body

    def current_version(self) -> int:
        current = self.load()
        return int(current["version"]) if current else 0


def checkpoint_file(workdir: str | Path) -> Path:
    return Path(workdir) / "checkpoints" / "checkpoint.json"


def statistics_snapshot_file(workdir: str | Path) -> Path:
    return Path(workdir) / "checkpoints" / "statistics.json"
