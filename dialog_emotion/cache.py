"""Content-addressed cache, key construction, versions, and commit locks."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_bytes, content_hash
from .config import PipelineConfig
from .errors import BusinessError, fail

CACHE_SCHEMA = "dialog-emotion-cache-entry-v1"
CONFIG_SCHEMA = "dialog-emotion-config-snapshot-v1"


def make_cache_key(
    stage: str,
    identity: dict[str, Any],
    audit: dict[str, Any] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Build a key from dependency identity only.

    ``audit`` records the global versions that make provenance inspectable but
    are not direct dependencies of the node. Transitive dependencies are still
    protected because their content hashes are part of ``identity``.
    """
    identity_payload = {"stage": stage, **identity}
    key = content_hash(identity_payload)
    components = {
        "identity": identity_payload,
        "audit_context": audit or {},
        "hash_algorithm": "sha256-canonical-json-v1",
    }
    return key, components


class ContentAddressedCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.locks_dir = self.root / "locks"
        self.tmp_dir = self.root / "tmp"
        for directory in (self.objects_dir, self.locks_dir, self.tmp_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def path_for(self, key: str) -> Path:
        self._validate_key(key)
        return self.objects_dir / key[:2] / f"{key}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self.path_for(key)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            entry = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            fail(
                "E_CACHE_CORRUPT",
                "cache entry cannot be decoded",
                key=key,
                path=str(path),
                error=str(exc),
            )
        self._verify_entry(key, entry, path)
        return entry

    def put(
        self,
        key: str,
        data: dict[str, Any],
        *,
        node: str,
        key_components: dict[str, Any],
        parents: dict[str, Any] | None = None,
        max_entries: int | None = None,
        replace: bool = False,
    ) -> dict[str, Any]:
        path = self.path_for(key)
        data_hash = content_hash(data)
        existing = self.get(key)
        if existing is not None:
            if existing["content_hash"] == data_hash and not replace:
                return existing
            if not replace:
                fail(
                    "E_CACHE_INTEGRITY",
                    "the same cache key produced different content",
                    key=key,
                    existing_content_hash=existing["content_hash"],
                    new_content_hash=data_hash,
                    node=node,
                )
        elif max_entries is not None and self.count_entries() >= max_entries:
            fail(
                "E_RESOURCE_LIMIT",
                "cache entry count exceeds configured limit",
                key=key,
                max_cache_entries=max_entries,
                node=node,
            )

        try:
            with self.stage_lock(key):
                current = self.get(key)
                if existing is None and current is not None:
                    fail(
                        "E_STAGE_BUSY",
                        "another task committed the same cache key first",
                        key=key,
                        node=node,
                    )
                version = 1 if current is None else int(current["version"]) + 1
                body = {
                    "schema": CACHE_SCHEMA,
                    "key": key,
                    "node": node,
                    "version": version,
                    "content_hash": data_hash,
                    "key_components": key_components,
                    "parents": parents or {},
                    "data": data,
                }
                entry = {**body, "checksum": content_hash(body)}
                tmp_path = self.tmp_dir / (
                    f"{key}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                tmp_path.write_bytes(canonical_bytes(entry) + b"\n")
                path.parent.mkdir(parents=True, exist_ok=True)
                os.replace(tmp_path, path)
                return entry
        except BusinessError:
            raise

    @contextmanager
    def stage_lock(self, key: str) -> Iterator[None]:
        self._validate_key(key)
        lock_path = self.locks_dir / f"{key}.lock"
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            fail(
                "E_STAGE_BUSY",
                "cache key is already being committed",
                key=key,
            )
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            descriptor = -1
            yield
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def count_entries(self) -> int:
        return sum(1 for _ in self.objects_dir.glob("*/*.json"))

    def list_entries(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for path in sorted(self.objects_dir.glob("*/*.json")):
            key = path.stem
            entry = self.get(key)
            if entry is not None:
                entries.append(
                    {
                        "key": key,
                        "node": entry["node"],
                        "version": entry["version"],
                        "content_hash": entry["content_hash"],
                        "key_components": entry["key_components"],
                        "parents": entry["parents"],
                        "path": str(path),
                    }
                )
        return entries

    def export_key_components(self, key: str) -> dict[str, Any]:
        entry = self.get(key)
        if entry is None:
            fail(
                "E_LINEAGE_BROKEN",
                "cache key does not exist",
                missing_key_level="cache_entry",
                key=key,
            )
        return entry["key_components"]

    def _verify_entry(
        self, key: str, entry: dict[str, Any], path: Path
    ) -> None:
        if not isinstance(entry, dict):
            fail(
                "E_CACHE_CORRUPT",
                "cache entry is not an object",
                key=key,
                path=str(path),
            )
        checksum = entry.get("checksum")
        body = {field: value for field, value in entry.items() if field != "checksum"}
        if checksum != content_hash(body):
            fail(
                "E_CACHE_CORRUPT",
                "cache entry checksum mismatch",
                key=key,
                path=str(path),
            )
        if entry.get("schema") != CACHE_SCHEMA or entry.get("key") != key:
            fail(
                "E_CACHE_CORRUPT",
                "cache entry schema or key mismatch",
                key=key,
                path=str(path),
            )
        if entry.get("content_hash") != content_hash(entry.get("data")):
            fail(
                "E_CACHE_CORRUPT",
                "cache entry content hash mismatch",
                key=key,
                path=str(path),
            )
        if not isinstance(entry.get("version"), int) or entry["version"] < 1:
            fail(
                "E_CACHE_CORRUPT",
                "cache entry version is invalid",
                key=key,
                path=str(path),
            )

    @staticmethod
    def _validate_key(key: str) -> None:
        if (
            len(key) != 64
            or any(character not in "0123456789abcdef" for character in key)
        ):
            fail(
                "E_CACHE_INVALID_KEY",
                "cache key must be a lowercase sha256 hex digest",
                key=key,
            )


class ConfigSnapshotStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.archive_dir = self.root / "archive"
        self.tmp_dir = self.root / "tmp"
        self.locks_dir = self.root / "locks"
        for directory in (self.root, self.archive_dir, self.tmp_dir, self.locks_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.current_path = self.root / "current.json"

    def commit(self, config: PipelineConfig) -> dict[str, Any]:
        config.validate()
        data = config.to_dict()
        config_hash = content_hash(data)
        lock_path = self.locks_dir / "config.lock"
        try:
            descriptor = os.open(
                lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
        except FileExistsError:
            fail(
                "E_STAGE_BUSY",
                "configuration snapshot is already being committed",
                key="config",
            )
        try:
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            os.close(descriptor)
            descriptor = -1
            current = self.load_current()
            if current is not None and current["content_hash"] == config_hash:
                return current
            version = 1 if current is None else int(current["version"]) + 1
            body = {
                "schema": CONFIG_SCHEMA,
                "version": version,
                "content_hash": config_hash,
                "config": data,
                "replacement": "whole-snapshot",
            }
            snapshot = {**body, "checksum": content_hash(body)}
            archive_path = self.archive_dir / f"v{version:08d}.json"
            payload = canonical_bytes(snapshot) + b"\n"
            archive_path.write_bytes(payload)
            tmp_path = self.tmp_dir / (
                f"config.{os.getpid()}.{threading.get_ident()}.tmp"
            )
            tmp_path.write_bytes(payload)
            os.replace(tmp_path, self.current_path)
            return snapshot
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def load_current(self) -> dict[str, Any] | None:
        if not self.current_path.exists():
            return None
        return self._load_path(self.current_path)

    def load_version(self, version: int) -> dict[str, Any]:
        path = self.archive_dir / f"v{version:08d}.json"
        if not path.exists():
            fail(
                "E_LINEAGE_BROKEN",
                "configuration snapshot version is missing",
                missing_key_level="config_snapshot",
                version=version,
            )
        return self._load_path(path)

    def _load_path(self, path: Path) -> dict[str, Any]:
        try:
            snapshot = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            fail(
                "E_CACHE_CORRUPT",
                "configuration snapshot cannot be decoded",
                path=str(path),
                error=str(exc),
            )
        checksum = snapshot.get("checksum")
        body = {key: value for key, value in snapshot.items() if key != "checksum"}
        if checksum != content_hash(body) or snapshot.get("schema") != CONFIG_SCHEMA:
            fail(
                "E_CACHE_CORRUPT",
                "configuration snapshot checksum mismatch",
                path=str(path),
            )
        if snapshot.get("content_hash") != content_hash(snapshot.get("config")):
            fail(
                "E_CACHE_CORRUPT",
                "configuration snapshot content hash mismatch",
                path=str(path),
            )
        return snapshot
