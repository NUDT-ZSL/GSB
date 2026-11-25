"""Atomic JSON snapshots, content cache, and non-blocking commit lock."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .codes import BusinessCode
from .errors import QCError
from .hashing import canonical_hash, hash_with_checksum, json_default, verify_checksum


def atomic_write_bytes(path: str | Path, data: bytes, *, durable: bool = True) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        if durable:
            _fsync_directory(path.parent)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            pass
        raise


def atomic_write_json(path: str | Path, payload: Any, *, durable: bool = True) -> None:
    data = (
        json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
            default=json_default,
        )
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, data, durable=durable)


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class CommitCoordinator:
    """In-process and cross-process non-blocking commit mutual exclusion."""

    def __init__(self, lock_path: str | Path):
        self.lock_path = Path(lock_path)
        self._thread_lock = threading.Lock()

    @contextmanager
    def lease(self, stage: str, owner: str = "local") -> Iterator[None]:
        if not self._thread_lock.acquire(blocking=False):
            raise QCError(
                BusinessCode.E_STAGE_BUSY,
                "another snapshot or verdict commit is already active",
                {"stage": stage, "lock_path": str(self.lock_path), "scope": "thread"},
            )
        descriptor: int | None = None
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise QCError(
                    BusinessCode.E_STAGE_BUSY,
                    "another process owns the commit lock",
                    {"stage": stage, "lock_path": str(self.lock_path), "scope": "process"},
                ) from exc
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"{owner}\n{stage}\n".encode("utf-8"))
            os.fsync(descriptor)
            yield
        finally:
            if descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            self._thread_lock.release()


class ChunkCache:
    def __init__(self, root: str | Path, max_entries: int = 20_000):
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self.hits = 0
        self.misses = 0
        self._known_keys = {path.stem for path in self.entries_dir.glob("*.json")}

    @staticmethod
    def make_key(payload: dict[str, Any]) -> str:
        return canonical_hash(payload)

    def path_for(self, key: str) -> Path:
        return self.entries_dir / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self.misses += 1
            return None
        if payload.get("cache_key") != key or not verify_checksum(payload):
            self.misses += 1
            return None
        self.hits += 1
        return payload.get("value")

    def put(self, key: str, value: dict[str, Any]) -> None:
        path = self.path_for(key)
        envelope = hash_with_checksum({"cache_key": key, "value": value})
        atomic_write_json(path, envelope, durable=False)
        self._known_keys.add(key)
        self._last_put_key = key
        self._evict()

    def _evict(self) -> None:
        while len(self._known_keys) > self.max_entries:
            # Content digests are stable; explicit minimum keeps eviction
            # deterministic without clocks, randomness, or insertion journals.
            evictable = self._known_keys - {getattr(self, "_last_put_key", None)}
            oldest_key = min(evictable or self._known_keys)
            self.path_for(oldest_key).unlink(missing_ok=True)
            self._known_keys.discard(oldest_key)

    def stats(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "hits": self.hits,
            "misses": self.misses,
            "entry_count": len(self._known_keys),
            "max_entries": self.max_entries,
        }


STAGES = ("envelope", "alignment", "drift", "report")


class StateStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.snapshots_dir = self.root / "snapshots"
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"
        self.commit_lock_path = self.root / "locks" / "commit.lock"
        self.coordinator = CommitCoordinator(self.commit_lock_path)

    def load_current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        current = json.loads(self.current_path.read_text(encoding="utf-8"))
        for stage, reference in current.get("snapshots", {}).items():
            snapshot = self.load_snapshot(reference)
            if snapshot.get("checksum") != reference.get("checksum") or not verify_checksum(snapshot):
                raise QCError(BusinessCode.E_STATE_CORRUPT, "committed snapshot checksum mismatch", {"stage": stage})
        return current

    def load_snapshot(self, reference: dict[str, Any]) -> dict[str, Any]:
        path = self.root / reference["path"]
        if not path.exists():
            raise QCError(BusinessCode.E_NOT_FOUND, "snapshot file is missing", {"path": str(path)})
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        if not verify_checksum(snapshot):
            raise QCError(BusinessCode.E_STATE_CORRUPT, "snapshot checksum verification failed", {"path": str(path)})
        return snapshot

    def commit_snapshot(self, stage: str, body: dict[str, Any], owner: str = "local") -> dict[str, Any]:
        with self.coordinator.lease(f"commit:{stage}", owner):
            return self._commit_snapshot_locked(stage, body)

    def _commit_snapshot_locked(self, stage: str, body: dict[str, Any]) -> dict[str, Any]:
        if stage not in STAGES:
            raise ValueError(f"unknown stage: {stage}")
        existing_current = self.load_current()
        current = existing_current or {
            "generation": body.get("generation", "initial"),
            "revision": 0,
            "snapshots": {},
        }
        same_generation = current.get("generation") == body.get("generation")
        if not same_generation:
            # Keep the global revision monotonic even across whole-generation
            # replacements.  This prevents new snapshot filenames from
            # overwriting immutable snapshots from the previous generation.
            previous_global_revision = int(current.get("revision", 0))
            current = {
                "generation": body["generation"],
                "revision": previous_global_revision,
                "snapshots": {},
            }

        revision = int(current["revision"]) + 1
        previous_version = 0
        if stage in current["snapshots"]:
            old_reference = current["snapshots"][stage]
            previous_version = int(old_reference.get("version", 0))
        version = previous_version + 1
        document = hash_with_checksum(
            {
                **body,
                "stage": stage,
                "version": version,
                "revision": revision,
                "committed": True,
            }
        )
        relative_path = Path("snapshots") / f"{revision:06d}-{stage}-v{version:03d}.json"
        atomic_write_json(self.root / relative_path, document)
        current["revision"] = revision
        current["snapshots"][stage] = {
            "stage": stage,
            "version": version,
            "revision": revision,
            "path": str(relative_path),
            "checksum": document["checksum"],
            "content_hash": body.get("content_hash", canonical_hash(body)),
        }
        atomic_write_json(self.current_path, current)
        return document

    def latest_snapshot(self, stage: str) -> dict[str, Any] | None:
        current = self.load_current()
        if not current or stage not in current.get("snapshots", {}):
            return None
        return self.load_snapshot(current["snapshots"][stage])
