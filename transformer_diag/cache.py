from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    canonical_json_bytes,
    read_json,
    sha256_bytes,
)
from .workspace import Workspace


class ContentAddressedCache:
    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self.paths = workspace.paths
        self.paths.ensure()
        raw = read_json(self.paths.cache_index, default={"schema": 1, "entries": {}})
        if (
            not isinstance(raw, dict)
            or raw.get("schema") != 1
            or not isinstance(raw.get("entries"), dict)
        ):
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache index is malformed",
                field=str(self.paths.cache_index),
            )
        self.index: dict[str, dict[str, Any]] = raw["entries"]
        versions = read_json(self.paths.versions, default={"artifact": 0, "snapshots": {}})
        if (
            not isinstance(versions, dict)
            or isinstance(versions.get("artifact", 0), bool)
            or not isinstance(versions.get("artifact", 0), int)
            or not isinstance(versions.get("snapshots", {}), dict)
        ):
            raise BusinessError(
                "E_CACHE_CORRUPT", "artifact version state is malformed", field=str(self.paths.versions)
            )
        self.versions = versions
        self.versions.setdefault("snapshots", {})
        self._dirty_index = False
        self._dirty_versions = False

    def _object_path(self, content_hash: str) -> Path:
        return self.paths.cache_objects / f"{content_hash}.bin"

    def _meta_path(self, content_hash: str) -> Path:
        return self.paths.cache_objects / f"{content_hash}.meta.json"

    def get(
        self,
        cache_key: str,
        *,
        expected_job_id: str | None = None,
        expected_stage: str | None = None,
    ) -> dict[str, Any] | None:
        entry = self.index.get(cache_key)
        if entry is None:
            return None
        content_hash = entry.get("content_hash")
        if not isinstance(content_hash, str) or len(content_hash) != 64:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache index entry has an invalid content hash",
                field="cache.content_hash",
                details={"cache_key": cache_key},
            )
        artifact_version = entry.get("artifact_version")
        if isinstance(artifact_version, bool) or not isinstance(artifact_version, int):
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache index entry has an invalid artifact version",
                field="cache.artifact_version",
                details={"cache_key": cache_key},
            )
        object_path = self._object_path(content_hash)
        meta_path = self._meta_path(content_hash)
        try:
            blob = object_path.read_bytes()
        except OSError as exc:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                f"cache object is missing or unreadable: {object_path}",
                field="cache.object",
                details={"cache_key": cache_key, "content_hash": content_hash},
            ) from exc
        actual_hash = sha256_bytes(blob)
        if actual_hash != content_hash:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache object checksum mismatch",
                field="cache.object",
                details={
                    "cache_key": cache_key,
                    "expected": content_hash,
                    "actual": actual_hash,
                },
            )
        meta = read_json(meta_path, code="E_CACHE_CORRUPT")
        if not isinstance(meta, dict) or meta.get("content_hash") != content_hash:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache object metadata checksum is missing or inconsistent",
                field="cache.meta",
                details={"cache_key": cache_key, "content_hash": content_hash},
            )
        try:
            payload = json.loads(blob.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                f"cache object is not canonical JSON: {exc}",
                field="cache.object",
                details={"cache_key": cache_key, "content_hash": content_hash},
            ) from exc
        if expected_job_id is not None and payload.get("job_id") != expected_job_id:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache object job_id does not match requested job",
                field="cache.job_id",
                details={"cache_key": cache_key, "expected": expected_job_id},
            )
        if expected_stage is not None and payload.get("stage") != expected_stage:
            raise BusinessError(
                "E_CACHE_CORRUPT",
                "cache object stage does not match requested stage",
                field="cache.stage",
                details={"cache_key": cache_key, "expected": expected_stage},
            )
        return {
            "payload": payload,
            "content_hash": content_hash,
            "artifact_version": artifact_version,
            "cache_key": cache_key,
            "entry": entry,
        }

    def put(
        self,
        cache_key: str,
        payload: dict[str, Any],
        *,
        stage: str,
        job_id: str,
    ) -> dict[str, Any]:
        blob = canonical_json_bytes(payload)
        content_hash = sha256_bytes(blob)
        object_path = self._object_path(content_hash)
        if object_path.exists():
            try:
                existing = object_path.read_bytes()
            except OSError as exc:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    f"cannot read existing cache object: {exc}",
                    field="cache.object",
                ) from exc
            if sha256_bytes(existing) != content_hash:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    "existing cache object is corrupt",
                    field="cache.object",
                    details={"content_hash": content_hash},
                )
            meta = read_json(self._meta_path(content_hash), code="E_CACHE_CORRUPT")
            if not isinstance(meta, dict) or meta.get("content_hash") != content_hash:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    "existing cache object metadata is corrupt",
                    field="cache.meta",
                    details={"content_hash": content_hash},
                )
        else:
            atomic_write_bytes(object_path, blob)
            atomic_write_json(
                self._meta_path(content_hash),
                {
                    "schema": 1,
                    "encoding": "canonical-json-v1",
                    "content_hash": content_hash,
                    "size": len(blob),
                },
                pretty=True,
            )

        existing_entry = self.index.get(cache_key)
        if existing_entry is not None:
            if existing_entry.get("content_hash") != content_hash:
                raise BusinessError(
                    "E_CACHE_CORRUPT",
                    "cache key is already bound to a different content hash",
                    field="cache.key",
                    details={
                        "cache_key": cache_key,
                        "existing": existing_entry.get("content_hash"),
                        "new": content_hash,
                    },
                )
            artifact_version = existing_entry.get("artifact_version")
        else:
            artifact_version = self.allocate_artifact_version()
            self.index[cache_key] = {
                "content_hash": content_hash,
                "stage": stage,
                "job_id": job_id,
                "artifact_version": artifact_version,
                "encoding": "canonical-json-v1",
            }
            self._dirty_index = True
        return {
            "payload": payload,
            "content_hash": content_hash,
            "artifact_version": artifact_version,
            "cache_key": cache_key,
            "entry": self.index[cache_key],
        }

    def allocate_artifact_version(self) -> int:
        current = int(self.versions.get("artifact", 0))
        current += 1
        self.versions["artifact"] = current
        self._dirty_versions = True
        return current

    def next_snapshot_version(self, kind: str, key: str, floor: int = 0) -> int:
        current = int(self.versions["snapshots"].get(f"{kind}/{key}", 0))
        current = max(current, floor) + 1
        self.versions["snapshots"][f"{kind}/{key}"] = current
        self._dirty_versions = True
        return current

    def observe_snapshot_version(self, kind: str, key: str, version: int) -> None:
        state_key = f"{kind}/{key}"
        current = int(self.versions["snapshots"].get(state_key, 0))
        if version > current:
            self.versions["snapshots"][state_key] = int(version)
            self._dirty_versions = True

    def save(self) -> None:
        if self._dirty_versions:
            atomic_write_json(self.paths.versions, self.versions, pretty=True)
            self._dirty_versions = False
        if self._dirty_index:
            atomic_write_json(
                self.paths.cache_index,
                {"schema": 1, "entries": self.index},
                pretty=True,
            )
            self._dirty_index = False

    def inspect(self, cache_key: str) -> dict[str, Any]:
        entry = self.index.get(cache_key)
        if entry is None:
            raise BusinessError(
                "E_JOB_INVALID",
                "cache key is not present",
                field="cache_key",
                details={"cache_key": cache_key},
            )
        result = self.get(cache_key)
        assert result is not None
        return {
            "cache_key": cache_key,
            "content_hash": result["content_hash"],
            "artifact_version": result["artifact_version"],
            "stage": entry.get("stage"),
            "job_id": entry.get("job_id"),
            "key_material": result["payload"].get("key_material"),
            "payload_schema": result["payload"].get("schema"),
        }

