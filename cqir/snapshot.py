from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Iterator

from .errors import fail
from .util import atomic_write_text, canonical_json, sha256_text

SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class VersionedSnapshotStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _safe(self, value: str) -> str:
        cleaned = SAFE_NAME.sub("_", value)
        if cleaned and cleaned == value:
            return cleaned
        return f"{cleaned[:48]}-{sha256_text(value)[:12]}"

    def _snapshot_path(self, namespace: str, key: str) -> Path:
        return self.root / self._safe(namespace) / f"{self._safe(key)}.snapshot.json"

    def _lock_path(self, namespace: str, key: str) -> Path:
        return self.root / self._safe(namespace) / f"{self._safe(key)}.lock"

    @contextlib.contextmanager
    def guard(self, namespace: str, key: str) -> Iterator[None]:
        lock_path = self._lock_path(namespace, key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            fail(
                "E_STAGE_BUSY",
                "another writer owns this cache key",
                {
                    "namespace": namespace,
                    "cache_key": key,
                    "lock_path": str(lock_path),
                },
            )
        try:
            owner = f"{os.getpid()}:{threading.get_ident()}".encode("ascii")
            os.write(descriptor, owner)
            os.close(descriptor)
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _checksum(self, version: int, payload: Any) -> str:
        return sha256_text(canonical_json({"version": version, "payload": payload}))

    def load(self, namespace: str, key: str) -> dict[str, Any] | None:
        path = self._snapshot_path(namespace, key)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            required = {
                "schema",
                "namespace",
                "cache_key",
                "version",
                "payload",
                "checksum",
            }
            missing = required - set(value)
            if missing:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot envelope is missing fields",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "path": str(path),
                        "missing": sorted(missing),
                    },
                )
            if value["schema"] != "cqir-snapshot/1":
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot schema is unsupported",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "path": str(path),
                        "schema": value["schema"],
                    },
                )
            if value["namespace"] != namespace or value["cache_key"] != key:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot identity does not match its location",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "path": str(path),
                    },
                )
            version = value["version"]
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot version is invalid",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "path": str(path),
                        "version": version,
                    },
                )
            expected = self._checksum(version, value["payload"])
            if value["checksum"] != expected:
                fail(
                    "E_SNAPSHOT_CORRUPT",
                    "snapshot checksum does not match its content",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "path": str(path),
                        "expected_checksum": expected,
                        "actual_checksum": value["checksum"],
                    },
                )
            return value
        except (OSError, ValueError, TypeError, KeyError) as exc:
            fail(
                "E_SNAPSHOT_CORRUPT",
                "snapshot cannot be parsed",
                {
                    "namespace": namespace,
                    "cache_key": key,
                    "path": str(path),
                    "reason": str(exc),
                },
            )

    def replace(
        self,
        namespace: str,
        key: str,
        payload: Any,
        version: int,
        expected_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> dict[str, Any]:
        with self.guard(namespace, key):
            current = self.load(namespace, key)
            if isinstance(version, bool) or not isinstance(version, int) or version < 1:
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    "snapshot version must be a positive monotonic integer",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "requested_version": version,
                    },
                )
            if current is not None:
                if version <= current["version"]:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "snapshot version would replace a newer or equal snapshot",
                        {
                            "namespace": namespace,
                            "cache_key": key,
                            "current_version": current["version"],
                            "requested_version": version,
                        },
                    )
                if expected_version is not None and expected_version != current["version"]:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "snapshot version changed before replacement",
                        {
                            "namespace": namespace,
                            "cache_key": key,
                            "expected_version": expected_version,
                            "current_version": current["version"],
                            "requested_version": version,
                        },
                    )
                if expected_checksum is not None and expected_checksum != current["checksum"]:
                    fail(
                        "E_SNAPSHOT_ROLLBACK",
                        "snapshot checksum changed before replacement",
                        {
                            "namespace": namespace,
                            "cache_key": key,
                            "expected_checksum": expected_checksum,
                            "current_checksum": current["checksum"],
                            "requested_version": version,
                        },
                    )
            elif expected_version not in (None, 0):
                fail(
                    "E_SNAPSHOT_ROLLBACK",
                    "expected an existing snapshot but none was found",
                    {
                        "namespace": namespace,
                        "cache_key": key,
                        "expected_version": expected_version,
                        "requested_version": version,
                    },
                )
            checksum = self._checksum(version, payload)
            envelope = {
                "schema": "cqir-snapshot/1",
                "namespace": namespace,
                "cache_key": key,
                "version": version,
                "payload": payload,
                "checksum": checksum,
            }
            atomic_write_text(
                self._snapshot_path(namespace, key),
                canonical_json(envelope) + "\n",
            )
            return {
                "namespace": namespace,
                "cache_key": key,
                "version": version,
                "checksum": checksum,
                "replaced_whole_snapshot": True,
            }
