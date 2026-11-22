"""Content-addressed node cache and bounded feature residency."""

from __future__ import annotations

import json
import os
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Tuple

from .canonical import (
    SCHEMA_VERSION,
    canonical_bytes,
    derive_cache_key,
    sha256_json,
    verify_sealed,
)
from .errors import (
    E_CACHE_CORRUPT,
    E_INVALID_INPUT,
    E_RESOURCE_LIMIT,
    BusinessError,
)


@dataclass(frozen=True)
class ResourcePolicy:
    max_feature_items: int = 256
    max_feature_bytes: int = 4 * 1024 * 1024
    max_parallel_nodes: int = 2
    memory_policy: str = "evict"
    allow_parallel_degrade: bool = True

    def __post_init__(self) -> None:
        if self.max_feature_items < 0:
            raise BusinessError(
                E_INVALID_INPUT,
                "max_feature_items must be non-negative",
                category="resource_policy",
            )
        if self.max_feature_bytes < 0:
            raise BusinessError(
                E_INVALID_INPUT,
                "max_feature_bytes must be non-negative",
                category="resource_policy",
            )
        if self.max_parallel_nodes < 1:
            raise BusinessError(
                E_INVALID_INPUT,
                "max_parallel_nodes must be at least 1",
                category="resource_policy",
            )
        if self.memory_policy not in ("evict", "error"):
            raise BusinessError(
                E_INVALID_INPUT,
                "memory_policy must be 'evict' or 'error'",
                category="resource_policy",
            )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "max_feature_items": self.max_feature_items,
            "max_feature_bytes": self.max_feature_bytes,
            "max_parallel_nodes": self.max_parallel_nodes,
            "memory_policy": self.memory_policy,
            "allow_parallel_degrade": self.allow_parallel_degrade,
        }


@dataclass(frozen=True)
class CacheEntry:
    node: str
    cache_key: str
    key_parts: Mapping[str, Any]
    artifact_sha256: str
    artifact: Mapping[str, Any]
    raw_bytes: bytes


class ArtifactCache:
    """Disk cache where both the key and every artifact are self-verifying."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, node: str, cache_key: str) -> Path:
        return self.root / node / "{0}.json".format(cache_key)

    def load(self, node: str, cache_key: str) -> Optional[CacheEntry]:
        path = self.path_for(node, cache_key)
        if not path.exists():
            return None
        try:
            raw = path.read_bytes()
            entry = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry cannot be decoded: {0}".format(exc),
                category="cache_decode",
                details={"node": node, "cache_key": cache_key},
            )
        if not isinstance(entry, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry must be a JSON object",
                category="cache_shape",
                details={"node": node, "cache_key": cache_key},
            )
        if entry.get("format") != SCHEMA_VERSION:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry schema is unsupported",
                category="cache_schema",
                details={"node": node, "cache_key": cache_key},
            )
        if entry.get("node") != node or entry.get("cache_key") != cache_key:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry index does not match its path",
                category="cache_index",
                details={"node": node, "cache_key": cache_key},
            )
        key_parts = entry.get("key_parts")
        if not isinstance(key_parts, dict):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry key_parts must be an object",
                category="cache_key_parts",
                details={"node": node, "cache_key": cache_key},
            )
        if derive_cache_key(key_parts) != cache_key:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache key cannot be derived from key_parts",
                category="cache_key",
                details={"node": node, "cache_key": cache_key},
            )
        artifact = entry.get("artifact")
        if not isinstance(artifact, dict) or not verify_sealed(artifact):
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache artifact content hash is invalid",
                category="cache_artifact_hash",
                details={"node": node, "cache_key": cache_key},
            )
        claimed_artifact_sha = entry.get("artifact_sha256")
        actual_artifact_sha = sha256_json(artifact)
        if claimed_artifact_sha != actual_artifact_sha:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache artifact digest does not match artifact_sha256",
                category="cache_artifact_digest",
                details={"node": node, "cache_key": cache_key},
            )
        if artifact.get("kind") != node:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache artifact kind does not match node",
                category="cache_kind",
                details={"node": node, "cache_key": cache_key},
            )
        return CacheEntry(
            node=node,
            cache_key=cache_key,
            key_parts=dict(key_parts),
            artifact_sha256=actual_artifact_sha,
            artifact=artifact,
            raw_bytes=canonical_bytes(entry),
        )

    def store(
        self,
        node: str,
        cache_key: str,
        key_parts: Mapping[str, Any],
        artifact: Mapping[str, Any],
    ) -> CacheEntry:
        if derive_cache_key(key_parts) != cache_key:
            raise BusinessError(
                E_INVALID_INPUT,
                "cache key does not match supplied key parts",
                category="cache_key",
            )
        if not verify_sealed(artifact):
            raise BusinessError(
                E_INVALID_INPUT,
                "refusing to store an unsealed artifact",
                category="cache_artifact_hash",
            )
        entry_payload = {
            "format": SCHEMA_VERSION,
            "node": node,
            "cache_key": cache_key,
            "key_parts": dict(key_parts),
            "artifact_sha256": sha256_json(artifact),
            "artifact": dict(artifact),
        }
        encoded = canonical_bytes(entry_payload)
        path = self.path_for(node, cache_key)
        if path.exists():
            existing = self.load(node, cache_key)
            if existing is None or canonical_bytes(existing.artifact) != canonical_bytes(
                artifact
            ):
                raise BusinessError(
                    E_CACHE_CORRUPT,
                    "same cache key maps to a different artifact",
                    category="cache_collision",
                    details={"node": node, "cache_key": cache_key},
                )
            return existing
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        with temporary.open("wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        return CacheEntry(
            node=node,
            cache_key=cache_key,
            key_parts=dict(key_parts),
            artifact_sha256=sha256_json(artifact),
            artifact=dict(artifact),
            raw_bytes=encoded,
        )

    def iter_entries(self) -> Iterator[CacheEntry]:
        for node in ("feature", "distance", "decision", "metrics"):
            node_dir = self.root / node
            if not node_dir.exists():
                continue
            for path in sorted(node_dir.glob("*.json")):
                entry = self.load(node, path.stem)
                if entry is not None:
                    yield entry

    def verify_all(self) -> Dict[str, Any]:
        counts = {"feature": 0, "distance": 0, "decision": 0, "metrics": 0}
        for entry in self.iter_entries():
            counts[entry.node] = counts.get(entry.node, 0) + 1
        return {"business_code": "OK", "verified_entries": sum(counts.values()), "by_node": counts}


class FeatureMemory:
    """Bounded LRU residency for feature artifacts only."""

    def __init__(self, policy: ResourcePolicy) -> None:
        self.policy = policy
        self._entries: "OrderedDict[str, Tuple[Mapping[str, Any], int]]" = OrderedDict()
        self._bytes = 0
        self.insertions = 0
        self.evictions = 0

    def get(self, cache_key: str) -> Optional[Mapping[str, Any]]:
        value = self._entries.get(cache_key)
        if value is None:
            return None
        self._entries.move_to_end(cache_key)
        return value[0]

    def put(
        self,
        cache_key: str,
        artifact: Mapping[str, Any],
        *,
        image_id: Optional[str] = None,
    ) -> None:
        if cache_key in self._entries:
            self._entries.move_to_end(cache_key)
            return
        size = len(canonical_bytes(artifact))
        if (
            size > self.policy.max_feature_bytes
            or self.policy.max_feature_items == 0
        ):
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "feature artifact exceeds the residency limit",
                image_id=image_id,
                category="feature_residency",
                details={
                    "artifact_bytes": size,
                    "max_feature_bytes": self.policy.max_feature_bytes,
                    "max_feature_items": self.policy.max_feature_items,
                },
            )
        while self._entries and (
            len(self._entries) >= self.policy.max_feature_items
            or self._bytes + size > self.policy.max_feature_bytes
        ):
            if self.policy.memory_policy == "error":
                raise BusinessError(
                    E_RESOURCE_LIMIT,
                    "feature residency limit would be exceeded",
                    image_id=image_id,
                    category="feature_residency",
                    details={
                        "resident_items": len(self._entries),
                        "resident_bytes": self._bytes,
                        "requested_bytes": size,
                        "max_feature_items": self.policy.max_feature_items,
                        "max_feature_bytes": self.policy.max_feature_bytes,
                    },
                )
            evicted_key, (_, evicted_size) = self._entries.popitem(last=False)
            self._bytes -= evicted_size
            self.evictions += 1
        if size > self.policy.max_feature_bytes:
            raise BusinessError(
                E_RESOURCE_LIMIT,
                "feature artifact cannot fit in feature residency",
                image_id=image_id,
                category="feature_residency",
                details={
                    "artifact_bytes": size,
                    "max_feature_bytes": self.policy.max_feature_bytes,
                },
            )
        self._entries[cache_key] = (dict(artifact), size)
        self._bytes += size
        self.insertions += 1

    def stats(self) -> Dict[str, Any]:
        return {
            "resident_items": len(self._entries),
            "resident_bytes": self._bytes,
            "insertions": self.insertions,
            "evictions": self.evictions,
            "max_feature_items": self.policy.max_feature_items,
            "max_feature_bytes": self.policy.max_feature_bytes,
            "memory_policy": self.policy.memory_policy,
        }
