"""Content-addressed cache, monotonic versions and versioned config snapshots."""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .canonical import hash_value, read_json, write_json_atomic
from .config import ResourceLimits
from .errors import (
    E_CACHE_BUSY,
    E_CACHE_CORRUPT,
    E_INCONSISTENT,
    E_RESOURCE_LIMIT,
    BusinessError,
    raise_error,
)
from .errors import E_INPUT_INVALID
from .keys import KEY_SCHEMA


CACHE_SCHEMA = "vem.cache-entry.v1"
CONFIG_SCHEMA = "vem.config-snapshot.v1"
MANIFEST_SCHEMA = "vem.store-manifest.v1"


def _read_checked_json(path: Path, kind: str) -> Any:
    try:
        return read_json(path)
    except Exception as exc:
        raise_error(
            E_CACHE_CORRUPT,
            f"{kind} cannot be parsed",
            path=str(path),
            reason=str(exc),
        )


def _checked_body(body: dict[str, Any], *, path: Path, kind: str) -> dict[str, Any]:
    checksum = body.get("checksum")
    unsigned = {key: value for key, value in body.items() if key != "checksum"}
    if not isinstance(checksum, str) or checksum != hash_value(unsigned):
        raise_error(
            E_CACHE_CORRUPT,
            f"{kind} checksum mismatch",
            path=str(path),
            expected=checksum,
            actual=hash_value(unsigned),
        )
    return body


class VersionedManifest:
    """A single monotonic version clock shared by entries and config snapshots."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "store-manifest.json"
        self.lock_path = self.root / ".store-manifest.lock"
        self._ensure()

    def _body(self, next_version: int, entry_count: int) -> dict[str, Any]:
        body = {
            "schema": MANIFEST_SCHEMA,
            "next_version": int(next_version),
            "entry_count": int(entry_count),
        }
        return {**body, "checksum": hash_value(body)}

    def _ensure(self) -> None:
        if self.path.exists():
            self.load()
            return
        with self._lock(timeout=5.0):
            if not self.path.exists():
                write_json_atomic(self.path, self._body(1, 0))
        self.load()

    @contextmanager
    def _lock(self, timeout: float = 5.0) -> Iterator[None]:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise_error(
                        E_CACHE_BUSY,
                        "timed out acquiring the version manifest lock",
                        lock=str(self.lock_path),
                    )
                time.sleep(0.002)
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass

    def load(self) -> dict[str, Any]:
        body = _read_checked_json(self.path, "store manifest")
        if not isinstance(body, dict):
            raise_error(E_CACHE_CORRUPT, "store manifest is not an object", path=str(self.path))
        _checked_body(body, path=self.path, kind="store manifest")
        if body.get("schema") != MANIFEST_SCHEMA:
            raise_error(
                E_CACHE_CORRUPT,
                "unexpected store manifest schema",
                path=str(self.path),
                schema=body.get("schema"),
            )
        if int(body.get("next_version", 0)) < 1 or int(body.get("entry_count", -1)) < 0:
            raise_error(E_CACHE_CORRUPT, "invalid store manifest counters", path=str(self.path))
        return body

    def allocate(
        self,
        *,
        entry_delta: int,
        limits: ResourceLimits | None = None,
        allow_degrade: bool = False,
    ) -> int | None:
        with self._lock():
            body = self.load()
            if entry_delta and limits is not None:
                if int(body["entry_count"]) + entry_delta > int(limits.max_cache_entries):
                    if allow_degrade and limits.on_limit == "degrade":
                        return None
                    raise_error(
                        E_RESOURCE_LIMIT,
                        "cache entry limit exceeded",
                        resource="max_cache_entries",
                        current=int(body["entry_count"]),
                        requested_delta=int(entry_delta),
                        limit=int(limits.max_cache_entries),
                    )
            version = int(body["next_version"])
            updated = self._body(version + 1, int(body["entry_count"]) + int(entry_delta))
            write_json_atomic(self.path, updated)
            return version

    def entry_count(self) -> int:
        return int(self.load()["entry_count"])


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    node: str
    version: int
    checksum: str
    key_components: dict[str, Any]
    context_versions: dict[str, Any]
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        body = {
            "schema": CACHE_SCHEMA,
            "cache_key": self.cache_key,
            "node": self.node,
            "version": self.version,
            "key_components": self.key_components,
            "context_versions": self.context_versions,
            "payload": self.payload,
        }
        return {**body, "checksum": self.checksum}

    @classmethod
    def build(
        cls,
        *,
        cache_key: str,
        node: str,
        version: int,
        key_components: dict[str, Any],
        context_versions: dict[str, Any],
        payload: dict[str, Any],
    ) -> "CacheEntry":
        unsigned = {
            "schema": CACHE_SCHEMA,
            "cache_key": cache_key,
            "node": node,
            "version": int(version),
            "key_components": key_components,
            "context_versions": context_versions,
            "payload": payload,
        }
        return cls(
            cache_key=cache_key,
            node=node,
            version=int(version),
            checksum=hash_value(unsigned),
            key_components=key_components,
            context_versions=context_versions,
            payload=payload,
        )

    @classmethod
    def from_dict(cls, body: dict[str, Any], *, path: Path) -> "CacheEntry":
        _checked_body(body, path=path, kind="cache entry")
        if body.get("schema") != CACHE_SCHEMA:
            raise_error(
                E_CACHE_CORRUPT,
                "unexpected cache entry schema",
                path=str(path),
                schema=body.get("schema"),
            )
        if not isinstance(body.get("payload"), dict) or not isinstance(body.get("cache_key"), str):
            raise_error(E_CACHE_CORRUPT, "malformed cache entry", path=str(path))
        return cls(
            cache_key=body["cache_key"],
            node=str(body["node"]),
            version=int(body["version"]),
            checksum=str(body["checksum"]),
            key_components=body["key_components"],
            context_versions=body.get("context_versions", {}),
            payload=body["payload"],
        )


class CacheStore:
    def __init__(
        self,
        root: str | Path,
        limits: ResourceLimits | None = None,
    ) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.limits = limits or ResourceLimits()
        self.manifest = VersionedManifest(self.root)

    def entry_path(self, cache_key: str) -> Path:
        return self.objects / cache_key[:2] / f"{cache_key}.json"

    def _lock_path(self, cache_key: str) -> Path:
        return self.objects / cache_key[:2] / f"{cache_key}.lock"

    @contextmanager
    def write_lock(self, cache_key: str) -> Iterator[None]:
        """Try once to own a cache-key write. No waiting is performed."""

        lock_path = self._lock_path(cache_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise_error(
                E_CACHE_BUSY,
                "another writer owns this cache key",
                cache_key=cache_key,
                lock=str(lock_path),
            )
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            try:
                os.unlink(lock_path)
            except FileNotFoundError:
                pass

    def get_optional(self, cache_key: str) -> CacheEntry | None:
        path = self.entry_path(cache_key)
        if not path.exists():
            return None
        body = _read_checked_json(path, "cache entry")
        if not isinstance(body, dict):
            raise_error(E_CACHE_CORRUPT, "cache entry is not an object", path=str(path))
        entry = CacheEntry.from_dict(body, path=path)
        if entry.cache_key != cache_key:
            raise_error(
                E_CACHE_CORRUPT,
                "cache entry key does not match its path",
                path=str(path),
                expected=cache_key,
                actual=entry.cache_key,
            )
        recomputed_key = hash_value({"schema": KEY_SCHEMA, **entry.key_components})
        if recomputed_key != entry.cache_key:
            raise_error(
                E_CACHE_CORRUPT,
                "cache key components do not reproduce the cache key",
                path=str(path),
                expected=recomputed_key,
                actual=entry.cache_key,
            )
        return entry

    def get(self, cache_key: str) -> CacheEntry:
        entry = self.get_optional(cache_key)
        if entry is None:
            raise BusinessError(
                "E_CACHE_MISS",
                "cache entry is missing",
                cache_key=cache_key,
            )
        return entry

    def put(
        self,
        *,
        cache_key: str,
        node: str,
        key_components: dict[str, Any],
        context_versions: dict[str, Any],
        payload: dict[str, Any],
        allow_degrade: bool = False,
    ) -> CacheEntry | None:
        recomputed_key = hash_value({"schema": KEY_SCHEMA, **key_components})
        if recomputed_key != cache_key:
            raise_error(
                E_INCONSISTENT,
                "cache key does not match its key components",
                node=node,
                expected=recomputed_key,
                actual=cache_key,
            )
        existing = self.get_optional(cache_key)
        if existing is not None:
            if hash_value(existing.payload) != hash_value(payload):
                raise_error(
                    E_INCONSISTENT,
                    "same cache key produced a different payload",
                    cache_key=cache_key,
                    node=node,
                )
            return existing
        with self.write_lock(cache_key):
            existing = self.get_optional(cache_key)
            if existing is not None:
                if hash_value(existing.payload) != hash_value(payload):
                    raise_error(
                        E_INCONSISTENT,
                        "same cache key produced a different payload",
                        cache_key=cache_key,
                        node=node,
                    )
                return existing
            version = self.manifest.allocate(
                entry_delta=1,
                limits=self.limits,
                allow_degrade=allow_degrade,
            )
            if version is None:
                return None
            entry = CacheEntry.build(
                cache_key=cache_key,
                node=node,
                version=version,
                key_components=key_components,
                context_versions=context_versions,
                payload=payload,
            )
            path = self.entry_path(cache_key)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_json_atomic(path, entry.to_dict())
            verified = self.get_optional(cache_key)
            if verified is None or verified.checksum != entry.checksum:
                raise_error(
                    E_CACHE_CORRUPT,
                    "cache entry failed read-after-write verification",
                    cache_key=cache_key,
                )
            return verified


class ConfigStore:
    """Whole-snapshot config storage with monotonic versions."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.directory = self.root / "configs"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.current_path = self.directory / "current.json"
        self.lock_path = self.directory / ".current.lock"
        self.manifest = VersionedManifest(self.root)

    def _history_path(self, version: int) -> Path:
        return self.directory / f"config-v{int(version):08d}.json"

    def _history_versions(self) -> list[int]:
        versions = []
        for path in self.directory.glob("config-v*.json"):
            try:
                versions.append(int(path.stem.split("-v")[-1]))
            except ValueError:
                continue
        return sorted(versions)

    def _build(self, version: int, body: dict[str, Any]) -> dict[str, Any]:
        unsigned = {
            "schema": CONFIG_SCHEMA,
            "version": int(version),
            "body_hash": hash_value(body),
            "body": body,
        }
        return {**unsigned, "checksum": hash_value(unsigned)}

    def _load_path(self, path: Path, expected_version: int | None = None) -> dict[str, Any]:
        snapshot = _read_checked_json(path, "config snapshot")
        if not isinstance(snapshot, dict):
            raise_error(E_CACHE_CORRUPT, "config snapshot is not an object", path=str(path))
        _checked_body(snapshot, path=path, kind="config snapshot")
        if snapshot.get("schema") != CONFIG_SCHEMA:
            raise_error(
                E_CACHE_CORRUPT,
                "unexpected config snapshot schema",
                path=str(path),
                schema=snapshot.get("schema"),
            )
        if snapshot.get("body_hash") != hash_value(snapshot.get("body")):
            raise_error(
                E_CACHE_CORRUPT,
                "config body checksum mismatch",
                path=str(path),
            )
        if expected_version is not None and int(snapshot["version"]) != int(expected_version):
            raise_error(
                E_CACHE_CORRUPT,
                "config version mismatch",
                path=str(path),
                expected=int(expected_version),
                actual=snapshot.get("version"),
            )
        return snapshot

    def current_optional(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            if self._history_versions():
                raise_error(
                    E_CACHE_CORRUPT,
                    "config history exists but the current pointer is missing",
                )
            return None
        snapshot = self._load_path(self.current_path)
        version = int(snapshot["version"])
        history_path = self._history_path(version)
        if not history_path.exists():
            raise_error(
                E_CACHE_CORRUPT,
                "current config snapshot has no corresponding history file",
                version=version,
                path=str(history_path),
            )
        history = self._load_path(history_path, expected_version=version)
        if history["checksum"] != snapshot["checksum"]:
            raise_error(
                E_CACHE_CORRUPT,
                "current config snapshot differs from its history snapshot",
                version=version,
            )
        versions = self._history_versions()
        if versions and max(versions) > version:
            raise_error(
                E_CACHE_CORRUPT,
                "current config snapshot is older than retained history",
                version=version,
                latest=max(versions),
            )
        return snapshot

    def load(self, version: int | None = None) -> dict[str, Any]:
        if version is None:
            snapshot = self.current_optional()
            if snapshot is None:
                raise BusinessError("E_CACHE_MISS", "no current config snapshot")
            return snapshot
        path = self._history_path(int(version))
        if not path.exists():
            raise BusinessError(
                "E_CACHE_MISS",
                "config snapshot version is missing",
                version=int(version),
            )
        return self._load_path(path, expected_version=int(version))

    @contextmanager
    def _scan_lock(self) -> Iterator[None]:
        try:
            fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise_error(
                E_CACHE_BUSY,
                "another writer is replacing the current config snapshot",
                lock=str(self.lock_path),
            )
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            try:
                os.unlink(self.lock_path)
            except FileNotFoundError:
                pass

    def put(self, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise_error(E_INPUT_INVALID, "config body must be an object")
        current = self.current_optional()
        if current is not None and current.get("body_hash") == hash_value(body):
            return current
        with self._scan_lock():
            current = self.current_optional()
            if current is not None and current.get("body_hash") == hash_value(body):
                return current
            version = self.manifest.allocate(entry_delta=0)
            assert version is not None
            snapshot = self._build(version, body)
            write_json_atomic(self._history_path(version), snapshot)
            # The current pointer is replaced with a complete snapshot, never
            # patched in place.
            write_json_atomic(self.current_path, snapshot)
            return self._load_path(self.current_path, expected_version=version)
