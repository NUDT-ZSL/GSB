"""Versioned atomic snapshots for repository, format definitions and answers.

Each of the three components carries a monotonically increasing version number
and a content checksum. A change in either triggers a *whole-component
replacement* (never an in-place patch). Old versions can never overwrite newer
ones (:data:`~diagwb.errors.E_SNAPSHOT_ROLLBACK`), checksum failures raise
:data:`~diagwb.errors.E_SNAPSHOT_CORRUPT`, and a non-blocking stage lock makes
the losing concurrent committer fail immediately with
:data:`~diagwb.errors.E_STAGE_BUSY` instead of waiting.
"""

from __future__ import annotations

import errno
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator

from .errors import (E_SNAPSHOT_CORRUPT, E_SNAPSHOT_ROLLBACK, E_STAGE_BUSY,
                     DiagError)
from .hashing import content_hash

REPOSITORY = "repository"
FORMAT_DEFS = "format_definitions"
ANSWERS = "answers"
COMPONENTS = (REPOSITORY, FORMAT_DEFS, ANSWERS)


def checksum(payload: Any) -> str:
    return content_hash(payload)


@dataclass
class Snapshot:
    component: str
    version: int
    checksum_value: str
    payload: Any

    def to_dict(self) -> Dict[str, Any]:
        return {"component": self.component, "version": self.version,
                "checksum": self.checksum_value, "payload": self.payload}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Snapshot":
        return cls(component=data["component"], version=int(data["version"]),
                   checksum_value=data["checksum"], payload=data["payload"])


class StageLock:
    """O_EXCL-based non-blocking single-committer lock."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._held = False

    def acquire(self) -> None:
        try:
            fd = os.open(str(self.lock_path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError as exc:
            if exc.errno in (errno.EEXIST, errno.EAGAIN):
                raise DiagError(
                    E_STAGE_BUSY,
                    "another snapshot commit is already in flight",
                    {"lock": str(self.lock_path)}) from exc
            raise
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        self._held = True

    def release(self) -> None:
        if self._held and self.lock_path.exists():
            self.lock_path.unlink()
        self._held = False


class SnapshotStore:
    """Persists versioned snapshots for the three components."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.stage_lock = StageLock(self.root / ".stage.lock")

    def _path(self, component: str, version: int) -> Path:
        return self.root / f"{component}.v{version}.json"

    def _current_path(self, component: str) -> Path:
        return self.root / f"{component}.current.json"

    def latest_version(self, component: str) -> int:
        current = self._current_path(component)
        if not current.exists():
            return 0
        return int(Snapshot.from_dict(
            json.loads(current.read_text(encoding="utf-8"))).version)

    @contextmanager
    def commit_stage(self) -> Iterator[None]:
        """Yield while holding the non-blocking stage lock."""
        self.stage_lock.acquire()
        try:
            yield
        finally:
            self.stage_lock.release()

    def commit(self, component: str, payload: Any,
               expected_version: int | None = None) -> Snapshot:
        """Validate and atomically publish a new snapshot version.

        Must be called inside :meth:`commit_stage`. The new version must be
        exactly one greater than the current version; equal or lower values
        are rejected as rollback attempts.
        """
        if not self.stage_lock._held:
            raise DiagError(E_STAGE_BUSY,
                            "commit requires the stage lock to be held",
                            {"component": component})
        if component not in COMPONENTS:
            raise DiagError(E_SNAPSHOT_CORRUPT,
                            "unknown snapshot component",
                            {"component": component, "version": 0})
        current_version = self.latest_version(component)
        new_version = current_version + 1
        if expected_version is not None and \
                expected_version != new_version:
            if expected_version <= current_version:
                raise DiagError(
                    E_SNAPSHOT_ROLLBACK,
                    "older snapshot cannot replace a newer one",
                    {"component": component,
                     "current_version": current_version,
                     "attempted_version": expected_version})
        new_checksum = checksum(payload)
        snapshot = Snapshot(component, new_version, new_checksum, payload)
        target = self._path(component, new_version)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot.to_dict(), sort_keys=True,
                                  indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, target)
        self._current_path(component).write_text(
            json.dumps(snapshot.to_dict(), sort_keys=True, indent=2,
                       ensure_ascii=False),
            encoding="utf-8")
        return snapshot

    def try_rollback(self, component: str, attempted_version: int) -> None:
        """Explicitly reject publishing a version <= the current one."""
        current = self.latest_version(component)
        if attempted_version <= current:
            raise DiagError(
                E_SNAPSHOT_ROLLBACK,
                "rollback attempt rejected; snapshots are append-only",
                {"component": component, "current_version": current,
                 "attempted_version": attempted_version})

    def load(self, component: str,
             version: int | None = None) -> Snapshot:
        """Load a snapshot, verifying its checksum and integrity."""
        path = (self._path(component, version)
                if version is not None else self._current_path(component))
        if not path.exists():
            raise DiagError(E_SNAPSHOT_CORRUPT,
                            "snapshot file is missing",
                            {"component": component,
                             "version": version or self.latest_version(
                                 component)})
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DiagError(E_SNAPSHOT_CORRUPT,
                            "snapshot is not valid JSON",
                            {"component": component,
                             "version": version or 0}) from exc
        snapshot = Snapshot.from_dict(data)
        recomputed = checksum(snapshot.payload)
        if recomputed != snapshot.checksum_value:
            raise DiagError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum does not match its payload",
                {"component": component, "version": snapshot.version,
                 "expected": snapshot.checksum_value,
                 "actual": recomputed})
        return snapshot

    def versions(self) -> Dict[str, int]:
        return {component: self.latest_version(component)
                for component in COMPONENTS}
