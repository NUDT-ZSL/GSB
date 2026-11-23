"""Region-level, content-addressed disk cache."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .errors import BusinessError, ErrorCode
from .hashing import array_content_hash, product_hash, sha256_json


@dataclass(frozen=True)
class CacheRecord:
    key: str
    node: str
    region_id: int
    artifact_hash: str
    components: dict[str, Any]
    product_metadata: dict[str, Any]
    artifact_file: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "node": self.node,
            "region_id": self.region_id,
            "artifact_hash": self.artifact_hash,
            "key_components": self.components,
            "product_metadata": self.product_metadata,
            "artifact_file": self.artifact_file,
        }


class ArtifactCache:
    """Stores arrays by the SHA-256 of their canonical pixel content."""

    SCHEMA = "RF-CACHE-V1"

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.records_dir = self.root / "records"
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.records_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _record_path(self, key: str) -> Path:
        return self.records_dir / f"{key}.json"

    def _object_path(self, artifact_hash: str) -> Path:
        return self.objects_dir / f"{artifact_hash}.npy"

    def lookup(self, key: str) -> CacheRecord | None:
        path = self._record_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            record = self._record_from_dict(payload)
        except BusinessError:
            raise
        except Exception as exc:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache record is unreadable",
                key=key,
                path=str(path),
                cause=type(exc).__name__,
            ) from exc
        if record.key != key:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache record key does not match its filename",
                key=key,
                record_key=record.key,
            )
        return record

    def load(self, key: str) -> tuple[np.ndarray, CacheRecord]:
        record = self.lookup(key)
        if record is None:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache record is missing for a requested key",
                key=key,
            )
        object_path = self._object_path(record.artifact_hash)
        if not object_path.exists():
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache artifact is missing",
                key=key,
                node=record.node,
                region_id=record.region_id,
                artifact_hash=record.artifact_hash,
            )
        try:
            array = np.load(object_path, allow_pickle=False)
        except Exception as exc:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache artifact is unreadable",
                key=key,
                node=record.node,
                region_id=record.region_id,
                artifact_hash=record.artifact_hash,
                cause=type(exc).__name__,
            ) from exc
        expected_array_hash = str(record.product_metadata.get("array_hash", ""))
        actual_array_hash = array_content_hash(array)
        if actual_array_hash != expected_array_hash:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache artifact content hash does not match its record",
                key=key,
                node=record.node,
                region_id=record.region_id,
                expected=expected_array_hash,
                actual=actual_array_hash,
            )
        expected_product = product_hash(array, record.product_metadata)
        if expected_product != record.artifact_hash:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache product metadata hash does not match its record",
                key=key,
                node=record.node,
                region_id=record.region_id,
            )
        return array, record

    def store(
        self,
        key: str,
        array: np.ndarray,
        node: str,
        region_id: int,
        components: dict[str, Any],
        product_metadata: dict[str, Any],
    ) -> CacheRecord:
        metadata = dict(product_metadata)
        metadata.setdefault("node", node)
        metadata.setdefault("region_id", region_id)
        metadata.setdefault("array_hash", array_content_hash(array))
        artifact_hash = product_hash(array, metadata)
        record = CacheRecord(
            key=key,
            node=node,
            region_id=region_id,
            artifact_hash=artifact_hash,
            components=dict(components),
            product_metadata=metadata,
            artifact_file=f"{artifact_hash}.npy",
        )
        return self._store_record(record, array)

    def _store_record(self, record: CacheRecord, array: np.ndarray) -> CacheRecord:
        with self._lock:
            object_path = self._object_path(record.artifact_hash)
            if object_path.exists():
                try:
                    existing = np.load(object_path, allow_pickle=False)
                except Exception as exc:
                    raise BusinessError(
                        ErrorCode.CACHE_CORRUPT,
                        "existing cache object is unreadable",
                        artifact_hash=record.artifact_hash,
                        node=record.node,
                        region_id=record.region_id,
                        cause=type(exc).__name__,
                    ) from exc
                if array_content_hash(existing) != record.product_metadata.get("array_hash"):
                    raise BusinessError(
                        ErrorCode.CACHE_CORRUPT,
                        "existing cache object has an unexpected content hash",
                        artifact_hash=record.artifact_hash,
                        node=record.node,
                        region_id=record.region_id,
                    )
            else:
                self._atomic_save_array(object_path, array)

            record_path = self._record_path(record.key)
            if record_path.exists():
                existing_record = self.lookup(record.key)
                if existing_record is None:
                    raise BusinessError(
                        ErrorCode.CACHE_CORRUPT,
                        "cache record disappeared during write",
                        key=record.key,
                    )
                if existing_record.artifact_hash != record.artifact_hash:
                    raise BusinessError(
                        ErrorCode.CACHE_CORRUPT,
                        "cache key already maps to a different artifact hash",
                        key=record.key,
                        expected=existing_record.artifact_hash,
                        actual=record.artifact_hash,
                    )
                return existing_record

            payload = {
                "schema": self.SCHEMA,
                "key": record.key,
                "node": record.node,
                "region_id": record.region_id,
                "artifact_hash": record.artifact_hash,
                "key_components": record.components,
                "product_metadata": record.product_metadata,
                "artifact_file": record.artifact_file,
            }
            encoded = (
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            self._atomic_write_bytes(record_path, encoded)
            return record

    def _atomic_save_array(self, path: Path, array: np.ndarray) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".npy", dir=self.objects_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                np.save(handle, np.ascontiguousarray(array), allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _atomic_write_bytes(self, path: Path, payload: bytes) -> None:
        fd, temp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=self.records_dir)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def _record_from_dict(self, payload: dict[str, Any]) -> CacheRecord:
        if payload.get("schema") != self.SCHEMA:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "unsupported cache record schema",
                schema=payload.get("schema"),
            )
        try:
            return CacheRecord(
                key=str(payload["key"]),
                node=str(payload["node"]),
                region_id=int(payload["region_id"]),
                artifact_hash=str(payload["artifact_hash"]),
                components=dict(payload["key_components"]),
                product_metadata=dict(payload["product_metadata"]),
                artifact_file=str(payload["artifact_file"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise BusinessError(
                ErrorCode.CACHE_CORRUPT,
                "cache record is missing required fields",
                cause=type(exc).__name__,
            ) from exc


def make_cache_entry(
    cache: ArtifactCache,
    key: str,
    array: np.ndarray,
    node: str,
    region_id: int,
    components: dict[str, Any],
    product_metadata: dict[str, Any],
) -> CacheRecord:
    return cache.store(
        key=key,
        array=array,
        node=node,
        region_id=region_id,
        components=components,
        product_metadata=product_metadata,
    )


def cache_key(components: dict[str, Any]) -> str:
    return sha256_json({"schema": "RF-KEY-V1", "components": components})
