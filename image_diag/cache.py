from __future__ import annotations

import io
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .deterministic import canonical_json, sha256_bytes
from .errors import BusinessError, E_CACHE_CORRUPT, E_CHECKPOINT_INVALID
from .graph import StageHooks
from .state import CheckpointStore


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _npy_bytes(array: np.ndarray) -> bytes:
    buffer = io.BytesIO()
    np.save(buffer, np.asarray(array), allow_pickle=False)
    return buffer.getvalue()


@dataclass
class CacheEntry:
    key: str
    content_hash: str
    object_path: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content_hash": self.content_hash,
            "object_path": self.object_path,
            "metadata": self.metadata,
        }


class ContentAddressedCache:
    """Per-node, content-addressed region cache stored below cache/objects."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.objects_dir = self.root / "objects"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.objects_dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def _entry_path(self, key: str) -> Path:
        return self.entries_dir / f"{key}.json"

    def get(self, key: str) -> tuple[np.ndarray | None, CacheEntry | None]:
        entry_path = self._entry_path(key)
        if not entry_path.exists():
            self.misses += 1
            return None, None
        try:
            entry_raw = json.loads(entry_path.read_text(encoding="utf-8"))
            entry = CacheEntry(
                key=str(entry_raw["key"]),
                content_hash=str(entry_raw["content_hash"]),
                object_path=str(entry_raw["object_path"]),
                metadata=dict(entry_raw.get("metadata", {})),
            )
            if entry.key != key:
                raise ValueError("cache key does not match entry path")
            object_path = self.root / entry.object_path
            payload = object_path.read_bytes()
            if sha256_bytes(payload) != entry.content_hash:
                raise ValueError("cache object checksum mismatch")
            array = np.load(io.BytesIO(payload), allow_pickle=False)
        except (OSError, KeyError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise BusinessError(
                E_CACHE_CORRUPT,
                "cache entry failed integrity verification",
                {"key": key, "reason": str(exc)},
                http_status=500,
            ) from exc
        self.hits += 1
        return np.asarray(array), entry

    def put(
        self,
        key: str,
        array: np.ndarray,
        metadata: dict[str, Any],
    ) -> CacheEntry:
        payload = _npy_bytes(array)
        content_hash = sha256_bytes(payload)
        relative_object = Path("objects") / f"{content_hash}.npy"
        object_path = self.root / relative_object
        if not object_path.exists():
            _atomic_write(object_path, payload)
        entry = CacheEntry(
            key=key,
            content_hash=content_hash,
            object_path=str(relative_object),
            metadata={**metadata, "content_hash": content_hash},
        )
        serialized = canonical_json(entry.as_dict()).encode("utf-8")
        _atomic_write(self._entry_path(key), serialized)
        self.writes += 1
        return entry

    def summary(self) -> dict[str, Any]:
        entries = sorted(self.entries_dir.glob("*.json"))
        return {
            "entries": len(entries),
            "objects": len(list(self.objects_dir.glob("*.npy"))),
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "hit_rate": (
                float(self.hits) / float(self.hits + self.misses)
                if self.hits + self.misses
                else 0.0
            ),
        }

    def list_entries(self) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for path in sorted(self.entries_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                results.append(
                    {
                        "key": raw["key"],
                        "content_hash": raw["content_hash"],
                        "stage": raw.get("metadata", {}).get("stage"),
                        "region": raw.get("metadata", {}).get("region"),
                    }
                )
            except (OSError, KeyError, json.JSONDecodeError):
                results.append({"key": path.stem, "corrupt": True})
        return results


class CacheHooks(StageHooks):
    def __init__(
        self,
        cache: ContentAddressedCache,
        checkpoints: CheckpointStore,
        *,
        run_key: str,
        fail_after: int | None = None,
        force_recompute: bool = False,
    ) -> None:
        self.cache = cache
        self.checkpoints = checkpoints
        self.run_key = run_key
        self.committed = 0
        self.fail_after = fail_after
        self.force_recompute = force_recompute
        self._checkpoints = {
            (item["stage"], item["region"]): item
            for item in checkpoints.load(run_key)
        }
        self.resumed_count = 0

    def load(
        self, stage: str, region_id: str, key: str
    ) -> tuple[np.ndarray | None, str | None, dict[str, Any] | None]:
        if self.force_recompute:
            self.cache.misses += 1
            return None, None, None
        checkpoint = self._checkpoints.get((stage, region_id))
        if checkpoint is not None:
            if checkpoint["cache_key"] != key:
                raise BusinessError(
                    E_CHECKPOINT_INVALID,
                    "checkpoint cache key does not match current dependency graph",
                    {
                        "run_key": self.run_key,
                        "stage": stage,
                        "region": region_id,
                    },
                    http_status=409,
                )
            array, entry = self.cache.get(key)
            if array is None or entry is None:
                raise BusinessError(
                    E_CHECKPOINT_INVALID,
                    "committed checkpoint artifact is missing from cache",
                    {
                        "run_key": self.run_key,
                        "stage": stage,
                        "region": region_id,
                        "cache_key": key,
                    },
                    http_status=409,
                )
            if entry.content_hash != checkpoint["content_hash"]:
                raise BusinessError(
                    E_CHECKPOINT_INVALID,
                    "committed checkpoint artifact checksum changed",
                    {
                        "run_key": self.run_key,
                        "stage": stage,
                        "region": region_id,
                    },
                    http_status=409,
                )
            self.resumed_count += 1
            return array, "resumed_checkpoint", entry.metadata
        array, entry = self.cache.get(key)
        if array is None or entry is None:
            return None, None, None
        return array, "cache_hit", entry.metadata

    def store(
        self,
        stage: str,
        region_id: str,
        key: str,
        artifact: np.ndarray,
        metadata: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        entry = self.cache.put(key, artifact, metadata)
        return entry.content_hash, entry.metadata

    def commit(self, stage: str, region_id: str, key: str, digest: str) -> None:
        result = self.checkpoints.save(
            self.run_key,
            stage=stage,
            region_id=region_id,
            cache_key=key,
            content_hash=digest,
        )
        if result["created"]:
            self.committed += 1
            if self.fail_after is not None and self.committed >= self.fail_after:
                os._exit(42)

    def status(self) -> dict[str, Any]:
        return {
            "run_key": self.run_key,
            "committed_this_process": self.committed,
            "resumed_checkpoints": self.resumed_count,
            "cache": self.cache.summary(),
        }
