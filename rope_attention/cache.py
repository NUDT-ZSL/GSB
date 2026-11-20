"""Process-safe content-addressed node cache with auditable key parts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .errors import (
    E_CACHE_CONFLICT,
    E_CACHE_CORRUPT,
    E_RESOURCE_LIMIT,
    RopeError,
)
from .locking import FileLock
from .util import (
    array_to_payload,
    atomic_write_bytes,
    canonical_json_bytes,
    hash_array,
    load_json_checked,
    payload_to_array,
    sha256_json,
)


@dataclass(frozen=True)
class NodeCacheResult:
    node: str
    case_id: str
    key: str
    key_parts: dict[str, Any]
    semantic_fingerprint: str
    cache_version: int | None
    entry_checksum: str | None
    payload_hash: str
    arrays: dict[str, np.ndarray]
    metadata: dict[str, Any]
    status: str
    source_key: str | None = None
    replayed: bool = False
    degradation: dict[str, Any] | None = None

    @property
    def reused(self) -> bool:
        return self.status.startswith("hit_")

    @property
    def computed(self) -> bool:
        return self.status.startswith("computed")

    def to_dict(self) -> dict[str, Any]:
        return {
            "array_hashes": {
                name: hash_array(array) for name, array in sorted(self.arrays.items())
            },
            "cache_version": self.cache_version,
            "case_id": self.case_id,
            "computed": self.computed,
            "degradation": self.degradation,
            "entry_checksum": self.entry_checksum,
            "key": self.key,
            "key_parts": self.key_parts,
            "node": self.node,
            "payload_hash": self.payload_hash,
            "replayed": self.replayed,
            "reused": self.reused,
            "semantic_fingerprint": self.semantic_fingerprint,
            "source_key": self.source_key,
            "status": self.status,
        }


def encode_payload(
    arrays: dict[str, np.ndarray],
    metadata: dict[str, Any],
) -> dict[str, Any]:
    return {
        "arrays": {
            name: array_to_payload(arrays[name]) for name in sorted(arrays)
        },
        "metadata": metadata,
    }


def decode_payload(payload: dict[str, Any]) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    arrays_payload = payload.get("arrays")
    metadata = payload.get("metadata")
    if not isinstance(arrays_payload, dict) or not isinstance(metadata, dict):
        raise RopeError(
            E_CACHE_CORRUPT,
            "cache payload has an invalid shape",
        )
    arrays = {
        str(name): payload_to_array(value)
        for name, value in sorted(arrays_payload.items())
    }
    return arrays, metadata


class CacheStore:
    """Immutable entries plus a monotonic store-level entry version."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.entries = self.root / "entries"
        self.semantic = self.root / "semantic"
        self.locks = self.root / "locks"
        self.entries.mkdir(parents=True, exist_ok=True)
        self.semantic.mkdir(parents=True, exist_ok=True)
        self.locks.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "cache_manifest.json"
        self.manifest_lock_path = self.root / ".manifest.lock"

    def key_for(self, node: str, key_parts: dict[str, Any]) -> str:
        return sha256_json(
            {
                "key_schema": "rope-node-cache-key/v1",
                "node": node,
                "parts": key_parts,
            }
        )

    def entry_path(self, key: str) -> Path:
        return self.entries / f"{key}.json"

    def semantic_path(self, node: str, fingerprint: str) -> Path:
        return self.semantic / node / f"{fingerprint}.json"

    def _lock_path(self, key: str) -> Path:
        return self.locks / f"{key}.lock"

    def _default_manifest(self) -> dict[str, Any]:
        return {
            "cache_version": 0,
            "entry_count": 0,
            "schema": "rope-cache-manifest/v1",
        }

    def _read_manifest(self) -> dict[str, Any]:
        if not self.manifest_path.exists():
            return self._default_manifest()
        return load_json_checked(self.manifest_path)

    def _write_manifest(self, body: dict[str, Any]) -> None:
        document = dict(body)
        document["checksum"] = sha256_json(body)
        atomic_write_bytes(
            self.manifest_path,
            canonical_json_bytes(document, pretty=True) + b"\n",
        )

    def _next_cache_version(self) -> int:
        with FileLock(self.manifest_lock_path):
            manifest = self._read_manifest()
            body = {
                "cache_version": int(manifest["cache_version"]) + 1,
                "entry_count": int(manifest["entry_count"]) + 1,
                "schema": "rope-cache-manifest/v1",
            }
            self._write_manifest(body)
            return body["cache_version"]

    def entry_count(self) -> int:
        return sum(1 for _ in self.entries.glob("*.json"))

    def cache_version(self) -> int:
        return int(self._read_manifest()["cache_version"])

    def diagnostics(self) -> dict[str, Any]:
        manifest = self._read_manifest()
        semantic_count = sum(1 for _ in self.semantic.glob("*/*.json"))
        return {
            "cache_version": int(manifest["cache_version"]),
            "declared_entry_count": int(manifest["entry_count"]),
            "entry_count": self.entry_count(),
            "root": str(self.root),
            "semantic_index_count": semantic_count,
        }

    def _entry_body(
        self,
        *,
        cache_version: int,
        node: str,
        case_id: str,
        key: str,
        key_parts: dict[str, Any],
        semantic_fingerprint: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "cache_version": cache_version,
            "case_id": case_id,
            "key": key,
            "key_parts": key_parts,
            "node": node,
            "payload": payload,
            "payload_hash": sha256_json(payload),
            "schema": "rope-node-cache-entry/v1",
            "semantic_fingerprint": semantic_fingerprint,
        }

    def _load_entry(
        self,
        key: str,
        *,
        node: str,
        case_id: str,
        key_parts: dict[str, Any],
        semantic_fingerprint: str,
    ) -> tuple[dict[str, Any], str]:
        path = self.entry_path(key)
        document = load_json_checked(path)
        if document.get("schema") != "rope-node-cache-entry/v1":
            raise RopeError(
                E_CACHE_CORRUPT,
                "cache entry schema is not supported",
                details={"path": str(path), "key": key},
            )
        mismatches = {}
        if document.get("key") != key:
            mismatches["key"] = document.get("key")
        if document.get("node") != node:
            mismatches["node"] = document.get("node")
        if document.get("case_id") != case_id:
            mismatches["case_id"] = document.get("case_id")
        if document.get("key_parts") != key_parts:
            mismatches["key_parts"] = "different"
        if document.get("semantic_fingerprint") != semantic_fingerprint:
            mismatches["semantic_fingerprint"] = document.get("semantic_fingerprint")
        if mismatches:
            raise RopeError(
                E_CACHE_CONFLICT,
                "cache entry identity does not match the requested key",
                details={"path": str(path), "key": key, "mismatches": mismatches},
            )
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise RopeError(
                E_CACHE_CORRUPT,
                "cache entry payload is missing",
                details={"path": str(path), "key": key},
            )
        payload_hash = sha256_json(payload)
        if document.get("payload_hash") != payload_hash:
            raise RopeError(
                E_CACHE_CORRUPT,
                "cache entry payload hash mismatch",
                details={"path": str(path), "key": key},
            )
        checksum = document.get("checksum")
        return document, str(checksum)

    def _materialize(
        self,
        *,
        node: str,
        case_id: str,
        key: str,
        key_parts: dict[str, Any],
        semantic_fingerprint: str,
        payload: dict[str, Any],
    ) -> tuple[int, str]:
        cache_version = self._next_cache_version()
        body = self._entry_body(
            cache_version=cache_version,
            node=node,
            case_id=case_id,
            key=key,
            key_parts=key_parts,
            semantic_fingerprint=semantic_fingerprint,
            payload=payload,
        )
        document = dict(body)
        checksum = sha256_json(body)
        document["checksum"] = checksum
        atomic_write_bytes(
            self.entry_path(key),
            canonical_json_bytes(document, pretty=True) + b"\n",
        )
        self._write_semantic_index(
            node=node,
            semantic_fingerprint=semantic_fingerprint,
            key=key,
            payload_hash=body["payload_hash"],
            cache_version=cache_version,
        )
        return cache_version, checksum

    def _write_semantic_index(
        self,
        *,
        node: str,
        semantic_fingerprint: str,
        key: str,
        payload_hash: str,
        cache_version: int,
    ) -> None:
        path = self.semantic_path(node, semantic_fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(path.with_suffix(".lock")):
            body = {
                "cache_key": key,
                "cache_version": cache_version,
                "node": node,
                "payload_hash": payload_hash,
                "schema": "rope-semantic-index/v1",
                "semantic_fingerprint": semantic_fingerprint,
            }
            document = dict(body)
            document["checksum"] = sha256_json(body)
            atomic_write_bytes(
                path,
                canonical_json_bytes(document, pretty=True) + b"\n",
            )

    def _read_semantic_alias(
        self,
        *,
        node: str,
        semantic_fingerprint: str,
    ) -> tuple[str, str] | None:
        path = self.semantic_path(node, semantic_fingerprint)
        if not path.exists():
            return None
        document = load_json_checked(path)
        if document.get("schema") != "rope-semantic-index/v1":
            raise RopeError(
                E_CACHE_CORRUPT,
                "semantic cache index schema is not supported",
                details={"path": str(path)},
            )
        if (
            document.get("node") != node
            or document.get("semantic_fingerprint") != semantic_fingerprint
        ):
            raise RopeError(
                E_CACHE_CORRUPT,
                "semantic cache index identity mismatch",
                details={"path": str(path)},
            )
        return str(document["cache_key"]), str(document["payload_hash"])

    def get_or_compute(
        self,
        *,
        node: str,
        case_id: str,
        key_parts: dict[str, Any],
        semantic_fingerprint: str,
        compute: Callable[[], tuple[dict[str, np.ndarray], dict[str, Any]]],
        max_entry_count: int | None = None,
        overflow_policy: str = "degrade",
        allow_semantic_alias: bool = False,
    ) -> NodeCacheResult:
        key = self.key_for(node, key_parts)
        with FileLock(self._lock_path(key)):
            if self.entry_path(key).exists():
                document, checksum = self._load_entry(
                    key,
                    node=node,
                    case_id=case_id,
                    key_parts=key_parts,
                    semantic_fingerprint=semantic_fingerprint,
                )
                arrays, metadata = decode_payload(document["payload"])
                return NodeCacheResult(
                    node=node,
                    case_id=case_id,
                    key=key,
                    key_parts=key_parts,
                    semantic_fingerprint=semantic_fingerprint,
                    cache_version=int(document["cache_version"]),
                    entry_checksum=checksum,
                    payload_hash=str(document["payload_hash"]),
                    arrays=arrays,
                    metadata=metadata,
                    status="hit_exact",
                    replayed=True,
                )

            alias = (
                self._read_semantic_alias(
                    node=node,
                    semantic_fingerprint=semantic_fingerprint,
                )
                if allow_semantic_alias
                else None
            )
            if alias is not None:
                source_key, expected_payload_hash = alias
                source_path = self.entry_path(source_key)
                if not source_path.exists():
                    raise RopeError(
                        E_CACHE_CORRUPT,
                        "semantic alias points to a missing cache entry",
                        details={
                            "source_key": source_key,
                            "index": str(
                                self.semantic_path(node, semantic_fingerprint)
                            ),
                        },
                    )
                source_document = load_json_checked(source_path)
                payload = source_document.get("payload")
                if not isinstance(payload, dict):
                    raise RopeError(
                        E_CACHE_CORRUPT,
                        "semantic alias source payload is missing",
                        details={"source_key": source_key},
                    )
                payload_hash = sha256_json(payload)
                if (
                    source_document.get("node") != node
                    or source_document.get("semantic_fingerprint")
                    != semantic_fingerprint
                    or payload_hash != expected_payload_hash
                    or source_document.get("payload_hash") != payload_hash
                ):
                    raise RopeError(
                        E_CACHE_CORRUPT,
                        "semantic alias source does not match its index",
                        details={"source_key": source_key},
                    )
                arrays, metadata = decode_payload(payload)
                if max_entry_count is not None and self.entry_count() >= max_entry_count:
                    if overflow_policy == "error":
                        raise RopeError(
                            E_RESOURCE_LIMIT,
                            "cache entry limit would be exceeded",
                            details={"max_entry_count": max_entry_count},
                        )
                    return NodeCacheResult(
                        node=node,
                        case_id=case_id,
                        key=key,
                        key_parts=key_parts,
                        semantic_fingerprint=semantic_fingerprint,
                        cache_version=None,
                        entry_checksum=None,
                        payload_hash=payload_hash,
                        arrays=arrays,
                        metadata=metadata,
                        status="hit_semantic_uncached",
                        source_key=source_key,
                        replayed=True,
                        degradation={
                            "category": "cache_entry_limit",
                            "max_entry_count": max_entry_count,
                        },
                    )
                cache_version, checksum = self._materialize(
                    node=node,
                    case_id=case_id,
                    key=key,
                    key_parts=key_parts,
                    semantic_fingerprint=semantic_fingerprint,
                    payload=payload,
                )
                return NodeCacheResult(
                    node=node,
                    case_id=case_id,
                    key=key,
                    key_parts=key_parts,
                    semantic_fingerprint=semantic_fingerprint,
                    cache_version=cache_version,
                    entry_checksum=checksum,
                    payload_hash=payload_hash,
                    arrays=arrays,
                    metadata=metadata,
                    status="hit_semantic",
                    source_key=source_key,
                    replayed=True,
                )

            if max_entry_count is not None and self.entry_count() >= max_entry_count:
                if overflow_policy == "error":
                    raise RopeError(
                        E_RESOURCE_LIMIT,
                        "cache entry limit would be exceeded",
                        details={"max_entry_count": max_entry_count},
                    )
                arrays, metadata = compute()
                encoded = encode_payload(arrays, metadata)
                return NodeCacheResult(
                    node=node,
                    case_id=case_id,
                    key=key,
                    key_parts=key_parts,
                    semantic_fingerprint=semantic_fingerprint,
                    cache_version=None,
                    entry_checksum=None,
                    payload_hash=sha256_json(encoded),
                    arrays=arrays,
                    metadata=metadata,
                    status="computed_uncached",
                    degradation={
                        "category": "cache_entry_limit",
                        "max_entry_count": max_entry_count,
                    },
                )

            arrays, metadata = compute()
            encoded = encode_payload(arrays, metadata)
            cache_version, checksum = self._materialize(
                node=node,
                case_id=case_id,
                key=key,
                key_parts=key_parts,
                semantic_fingerprint=semantic_fingerprint,
                payload=encoded,
            )
            return NodeCacheResult(
                node=node,
                case_id=case_id,
                key=key,
                key_parts=key_parts,
                semantic_fingerprint=semantic_fingerprint,
                cache_version=cache_version,
                entry_checksum=checksum,
                payload_hash=sha256_json(encoded),
                arrays=arrays,
                metadata=metadata,
                status="computed",
            )

    def read_entry(self, key: str) -> dict[str, Any]:
        return load_json_checked(self.entry_path(key))

    def verify_all_entries(self) -> dict[str, Any]:
        checked = 0
        for path in sorted(self.entries.glob("*.json")):
            key = path.stem
            document = load_json_checked(path)
            if document.get("key") != key:
                raise RopeError(
                    E_CACHE_CORRUPT,
                    "cache entry filename and key field differ",
                    details={"path": str(path), "field": document.get("key")},
                )
            payload = document.get("payload")
            if not isinstance(payload, dict) or sha256_json(payload) != document.get(
                "payload_hash"
            ):
                raise RopeError(
                    E_CACHE_CORRUPT,
                    "cache entry payload hash mismatch",
                    details={"path": str(path)},
                )
            decode_payload(payload)
            checked += 1
        return {"checked_entries": checked, "ok": True}

