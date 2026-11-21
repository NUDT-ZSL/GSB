from __future__ import annotations

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from .cache import CacheMiss, CacheStore
from .canonical import canonical_json_bytes, sha256_json
from .config import config_fingerprint
from .errors import (
    BusinessError,
    E_CACHE_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)


class SnapshotStore:
    """Versioned whole-snapshot replacement with content verification."""

    def __init__(self, root: str | Path, cache: CacheStore) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.cache = cache
        self.current_path = self.root / "current.json"
        self.write_lock_path = self.root / ".snapshot.lock"

    def snapshot_path(self, version: int) -> Path:
        return self.root / f"snapshot-{version:08d}.json"

    @contextmanager
    def _write_lock(self) -> Iterator[None]:
        try:
            descriptor = os.open(
                self.write_lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
        except FileExistsError as exc:
            raise BusinessError(
                E_STAGE_BUSY,
                "another task is replacing the runtime snapshot",
                node="snapshot",
            ) from exc
        try:
            os.write(descriptor, str(os.getpid()).encode("ascii"))
            yield
        finally:
            os.close(descriptor)
            try:
                self.write_lock_path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _atomic_write(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _collect_entries(
        self,
        cache_keys: Iterable[str],
    ) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        manifest: dict[str, dict[str, Any]] = {}
        entries: dict[str, dict[str, Any]] = {}
        for key in sorted(set(cache_keys)):
            try:
                entry = self.cache.get_entry(key)
            except CacheMiss as exc:
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot references a missing cache entry",
                    node="snapshot",
                    details={"key": key},
                ) from exc
            entries[key] = entry
            manifest[key] = {
                "entry_checksum": entry["entry_checksum"],
                "payload_hash": entry["payload_hash"],
                "stage": entry["stage"],
                "node_id": entry["node_id"],
            }
        return manifest, entries

    def _build_snapshot(
        self,
        *,
        version: int,
        config: dict[str, Any],
        cache_keys: Iterable[str],
    ) -> dict[str, Any]:
        manifest, entries = self._collect_entries(cache_keys)
        body = {
            "schema_version": 1,
            "version": version,
            "config": config,
            "config_fingerprint": config_fingerprint(config),
            "cache_manifest": manifest,
            "cache_entries": entries,
        }
        snapshot = dict(body)
        snapshot["checksum"] = sha256_json(body)
        return snapshot

    def _verify_snapshot(self, snapshot: dict[str, Any]) -> None:
        if not isinstance(snapshot, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot must be an object",
                node="snapshot",
            )
        checksum = snapshot.get("checksum")
        body = {
            field: value
            for field, value in snapshot.items()
            if field != "checksum"
        }
        if checksum != sha256_json(body):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot checksum mismatch",
                node="snapshot",
                details={"version": snapshot.get("version")},
            )
        if not isinstance(snapshot.get("version"), int):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot version must be an integer",
                node="snapshot",
            )
        if snapshot.get("config_fingerprint") != config_fingerprint(
            snapshot.get("config", {})
        ):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot config fingerprint mismatch",
                node="snapshot",
            )
        manifest = snapshot.get("cache_manifest")
        if not isinstance(manifest, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot cache manifest must be an object",
                node="snapshot",
            )
        embedded_entries = snapshot.get("cache_entries")
        if not isinstance(embedded_entries, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot embedded cache entries must be an object",
                node="snapshot",
            )
        if set(embedded_entries) != set(manifest):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot manifest and embedded cache keys differ",
                node="snapshot",
            )
        for key, expected in manifest.items():
            try:
                entry = self.cache.get_entry(key)
            except CacheMiss as exc:
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot cache entry is missing",
                    node="snapshot",
                    details={"key": key},
                ) from exc
            if not isinstance(expected, dict):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot manifest entry must be an object",
                    node="snapshot",
                    details={"key": key},
                )
            if expected.get("entry_checksum") != entry.get("entry_checksum"):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot cache entry checksum changed",
                    node="snapshot",
                    details={"key": key},
                )
            if expected.get("payload_hash") != entry.get("payload_hash"):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot cache payload changed",
                    node="snapshot",
                    details={"key": key},
                )
            embedded = embedded_entries.get(key)
            if not isinstance(embedded, dict):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot embedded cache entry must be an object",
                    node="snapshot",
                    details={"key": key},
                )
            try:
                self.cache.verify_entry(key, embedded)
            except BusinessError as exc:
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot embedded cache entry failed verification",
                    node="snapshot",
                    details={"key": key, "cause": exc.code},
                ) from exc
            if embedded.get("entry_checksum") != expected.get("entry_checksum"):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "snapshot embedded entry checksum differs from manifest",
                    node="snapshot",
                    details={"key": key},
                )

    def _read_snapshot_file(self, path: Path) -> dict[str, Any]:
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot file is missing",
                node="snapshot",
                details={"path": str(path)},
            ) from exc
        try:
            snapshot = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot file is not valid JSON",
                node="snapshot",
                details={"path": str(path)},
            ) from exc
        self._verify_snapshot(snapshot)
        return snapshot

    def _read_current_pointer(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        try:
            pointer = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "current snapshot pointer is not valid JSON",
                node="snapshot",
            ) from exc
        if not isinstance(pointer, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "current snapshot pointer must be an object",
                node="snapshot",
            )
        return pointer

    def load_current(self) -> dict[str, Any] | None:
        pointer = self._read_current_pointer()
        if pointer is None:
            return None
        version = pointer.get("version")
        checksum = pointer.get("checksum")
        if not isinstance(version, int) or not isinstance(checksum, str):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "current snapshot pointer fields are invalid",
                node="snapshot",
            )
        snapshot = self._read_snapshot_file(self.snapshot_path(version))
        if snapshot.get("version") != version:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot version does not match current pointer",
                node="snapshot",
            )
        if snapshot.get("checksum") != checksum:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "snapshot checksum does not match current pointer",
                node="snapshot",
            )
        return snapshot

    def commit(
        self,
        *,
        config: dict[str, Any],
        cache_keys: Iterable[str],
        version: int | None = None,
    ) -> dict[str, Any]:
        with self._write_lock():
            current = self.load_current()
            current_version = int(current["version"]) if current else 0
            new_version = current_version + 1 if version is None else int(version)
            if new_version <= current_version:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot version must increase monotonically",
                    node="snapshot",
                    details={
                        "current_version": current_version,
                        "requested_version": new_version,
                    },
                )

            snapshot = self._build_snapshot(
                version=new_version,
                config=config,
                cache_keys=cache_keys,
            )
            self._verify_snapshot(snapshot)
            self._atomic_write(
                self.snapshot_path(new_version),
                canonical_json_bytes(snapshot),
            )
            pointer = {
                "version": new_version,
                "checksum": snapshot["checksum"],
            }
            self._atomic_write(self.current_path, canonical_json_bytes(pointer))
            return snapshot

    def apply(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Apply an externally supplied higher-version snapshot as one replacement."""

        self._verify_snapshot(snapshot)
        with self._write_lock():
            current = self.load_current()
            current_version = int(current["version"]) if current else 0
            version = int(snapshot["version"])
            if version <= current_version:
                raise BusinessError(
                    E_SNAPSHOT_ROLLBACK,
                    "cannot apply a snapshot at or below the current version",
                    node="snapshot",
                    details={
                        "current_version": current_version,
                        "requested_version": version,
                    },
                )
            self._atomic_write(
                self.snapshot_path(version),
                canonical_json_bytes(snapshot),
            )
            pointer = {
                "version": version,
                "checksum": snapshot["checksum"],
            }
            self._atomic_write(self.current_path, canonical_json_bytes(pointer))
            return snapshot
