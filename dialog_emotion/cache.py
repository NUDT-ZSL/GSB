from __future__ import annotations

import errno
import fcntl
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import (
    DomainError,
    E_CACHE_BUSY,
    E_CACHE_CORRUPT,
    E_CACHE_KEY_MISMATCH,
    E_RESOURCE_LIMIT,
)
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    read_json,
    sha256_text,
    sha256_value,
)


def compute_cache_key(key_parts: list[dict[str, Any]]) -> str:
    return sha256_value({"cache_key_schema": "key-v1", "parts": key_parts})


def _safe_name(key: str) -> str:
    digest = key.split(":", 1)[-1]
    return digest


class WriteClaim:
    def __init__(self, store: "CacheStore", key: str) -> None:
        self.store = store
        self.key = key
        self._used = False

    def commit(
        self,
        node: str,
        payload: dict[str, Any],
        key_parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if self._used:
            raise RuntimeError("write claim already used")
        self._used = True
        return self.store._commit_locked(self.key, node, payload, key_parts)


class CacheStore:
    def __init__(
        self,
        root: str | Path,
        max_entries: int | None = None,
        read_only: bool = False,
    ) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.locks_dir = self.root / "locks"
        self.snapshot_path = self.root / "snapshot.json"
        self.snapshot_lock_path = self.root / "snapshot.lock"
        self.max_entries = max_entries
        self.read_only = read_only
        if not read_only:
            self.root.mkdir(parents=True, exist_ok=True)
            self.entries_dir.mkdir(exist_ok=True)
            self.locks_dir.mkdir(exist_ok=True)
        self._thread_lock = threading.RLock()

    def entry_path(self, key: str) -> Path:
        name = _safe_name(key)
        return self.entries_dir / name[:2] / f"{name}.json"

    def lock_path(self, key: str) -> Path:
        return self.locks_dir / f"{_safe_name(key)}.lock"

    def _snapshot_version_guess(self) -> int | None:
        try:
            if not self.snapshot_path.exists():
                return None
            document = read_json(self.snapshot_path)
            return int(document.get("version")) if document.get("version") is not None else None
        except Exception:
            return None

    def _corrupt(self, key: str, message: str, version: int | None = None) -> DomainError:
        return DomainError(
            E_CACHE_CORRUPT,
            message,
            entry=key,
            version=version if version is not None else self._snapshot_version_guess(),
        )

    def _key_mismatch(self, key: str, message: str, **details: Any) -> DomainError:
        return DomainError(
            E_CACHE_KEY_MISMATCH,
            message,
            entry=key,
            key=key,
            **details,
        )

    def _verify_entry(self, expected_key: str, document: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(document, dict):
            raise self._corrupt(expected_key, "cache entry is not a JSON object")
        version = document.get("version")
        if document.get("schema_version") != "cache-entry-v1":
            raise self._corrupt(expected_key, "cache entry schema is invalid", version)
        if document.get("key") != expected_key:
            raise self._key_mismatch(
                expected_key,
                "cache entry key does not match the requested key",
                stored_key=document.get("key"),
            )
        key_parts = document.get("key_parts")
        if not isinstance(key_parts, list):
            raise self._corrupt(expected_key, "cache entry key_parts is invalid", version)
        derived_key = compute_cache_key(key_parts)
        if derived_key != expected_key:
            raise self._key_mismatch(
                expected_key,
                "cache key cannot be reproduced from key composition details",
                derived_key=derived_key,
            )
        artifact_hash = document.get("artifact_hash")
        pair_checksum = document.get("pair_checksum")
        if not isinstance(artifact_hash, str) or not isinstance(pair_checksum, str):
            raise self._corrupt(expected_key, "cache entry hash metadata is missing", version)
        expected_pair = sha256_text(f"{expected_key}\n{artifact_hash}\n{version}")
        if pair_checksum != expected_pair:
            raise self._key_mismatch(
                expected_key,
                "cache key and artifact hash cannot be cross-verified",
                artifact_hash=artifact_hash,
            )
        record_checksum = document.get("record_checksum")
        body = {
            key: value
            for key, value in document.items()
            if key not in {"record_checksum", "pair_checksum"}
        }
        if record_checksum != sha256_value(body):
            raise self._corrupt(expected_key, "cache entry checksum mismatch", version)
        payload = document.get("payload")
        actual_artifact_hash = sha256_value(payload)
        if actual_artifact_hash != artifact_hash:
            raise self._corrupt(expected_key, "cache artifact hash mismatch", version)
        return document

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.entry_path(key)
        if not path.exists():
            return None
        try:
            document = read_json(path)
        except Exception as exc:
            raise self._corrupt(key, "cache entry is not valid JSON") from exc
        return self._verify_entry(key, document)

    def _load_snapshot(self) -> dict[str, Any]:
        if not self.snapshot_path.exists():
            return {
                "schema_version": "cache-snapshot-v1",
                "version": 0,
                "entry_count": 0,
                "entries": [],
                "checksum": sha256_value(
                    {
                        "schema_version": "cache-snapshot-v1",
                        "version": 0,
                        "entry_count": 0,
                        "entries": [],
                    }
                ),
            }
        try:
            document = read_json(self.snapshot_path)
        except Exception as exc:
            raise self._corrupt("snapshot", "cache snapshot is not valid JSON") from exc
        if not isinstance(document, dict) or document.get("schema_version") != "cache-snapshot-v1":
            raise self._corrupt("snapshot", "cache snapshot schema is invalid", document.get("version"))
        checksum = document.get("checksum")
        body = {key: value for key, value in document.items() if key != "checksum"}
        if checksum != sha256_value(body):
            raise self._corrupt("snapshot", "cache snapshot checksum mismatch", document.get("version"))
        if not isinstance(document.get("version"), int) or document["version"] < 1:
            raise self._corrupt("snapshot", "cache snapshot version is invalid", document.get("version"))
        return document

    def snapshot(self) -> dict[str, Any]:
        return self._load_snapshot()

    def _scan_entry_summaries(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for path in sorted(self.entries_dir.glob("*/*.json")):
            try:
                document = read_json(path)
            except Exception as exc:
                raise self._corrupt("snapshot-scan", f"cannot read cache entry {path.name}") from exc
            summaries.append(
                {
                    "key": document.get("key"),
                    "node": document.get("node"),
                    "artifact_hash": document.get("artifact_hash"),
                    "version": document.get("version"),
                    "record_checksum": document.get("record_checksum"),
                }
            )
        summaries.sort(key=lambda item: str(item.get("key")))
        return summaries

    def _write_snapshot(self, version: int) -> dict[str, Any]:
        entries = self._scan_entry_summaries()
        body = {
            "schema_version": "cache-snapshot-v1",
            "version": version,
            "entry_count": len(entries),
            "entries": entries,
        }
        document = dict(body)
        document["checksum"] = sha256_value(body)
        atomic_write_json(self.snapshot_path, document)
        return document

    def _current_entry_count(self) -> int:
        snapshot = self._load_snapshot()
        return int(snapshot.get("entry_count", 0))

    @contextmanager
    def write_guard(self, key: str) -> Iterator[WriteClaim]:
        if self.read_only:
            raise RuntimeError("cache store is read-only")
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.lock_path(key)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                raise DomainError(
                    E_CACHE_BUSY,
                    "another writer currently owns this cache key",
                    key=key,
                    lock_path=str(lock_path),
                ) from exc
            raise
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            yield WriteClaim(self, key)
        finally:
            os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _commit_locked(
        self,
        key: str,
        node: str,
        payload: dict[str, Any],
        key_parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = self.get(key)
        if existing is not None:
            if existing.get("node") != node:
                raise self._key_mismatch(
                    key,
                    "cache entry node does not match the requested node",
                    expected_node=node,
                    stored_node=existing.get("node"),
                )
            return existing
        if compute_cache_key(key_parts) != key:
            raise self._key_mismatch(key, "provided key parts do not reproduce the key")
        with self._thread_lock:
            lock_handle = self.snapshot_lock_path.open("a+b")
            try:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                snapshot = self._load_snapshot()
                if self.max_entries is not None and snapshot["entry_count"] >= self.max_entries:
                    raise DomainError(
                        E_RESOURCE_LIMIT,
                        "cache entry limit has been reached",
                        category="cache_entries",
                        entry_count=snapshot["entry_count"],
                        limit=self.max_entries,
                        key=key,
                    )
                version = int(snapshot["version"]) + 1
                artifact_hash = sha256_value(payload)
                entry_body = {
                    "schema_version": "cache-entry-v1",
                    "node": node,
                    "key": key,
                    "key_parts": key_parts,
                    "payload": payload,
                    "artifact_hash": artifact_hash,
                    "version": version,
                }
                entry = dict(entry_body)
                entry["record_checksum"] = sha256_value(entry_body)
                entry["pair_checksum"] = sha256_text(f"{key}\n{artifact_hash}\n{version}")
                atomic_write_bytes(self.entry_path(key), canonical_json_bytes(entry))
                self._write_snapshot(version)
                return entry
            finally:
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_handle.close()

    def put(
        self,
        key: str,
        node: str,
        payload: dict[str, Any],
        key_parts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        existing = self.get(key)
        if existing is not None:
            if existing.get("node") != node:
                raise self._key_mismatch(
                    key,
                    "cache entry node does not match the requested node",
                    expected_node=node,
                    stored_node=existing.get("node"),
                )
            return existing
        with self.write_guard(key) as claim:
            return claim.commit(node, payload, key_parts)

    def entry_count(self) -> int:
        return self._current_entry_count()

    def snapshot_summary(self) -> dict[str, Any]:
        snapshot = self._load_snapshot()
        return {
            "version": snapshot["version"],
            "entry_count": snapshot["entry_count"],
            "checksum": snapshot["checksum"],
        }
