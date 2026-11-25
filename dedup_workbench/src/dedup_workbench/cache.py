"""Content-addressed disk cache with a bounded resident-memory tier."""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Callable

from .canonical import canonical_bytes, hash_json, sha256_bytes
from .errors import E_RESOURCE_LIMIT, WorkbenchError


class ContentAddressedCache:
    def __init__(
        self,
        root: str | Path,
        *,
        max_memory_bytes: int = 8 * 1024 * 1024,
        overflow_policy: str = "evict_then_spill",
    ) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.max_memory_bytes = int(max_memory_bytes)
        self.overflow_policy = overflow_policy
        self._resident: dict[str, bytes] = {}
        self._priorities: dict[str, int] = {}
        self.resident_bytes = 0
        self.hits = 0
        self.misses = 0
        self.evictions: list[dict[str, Any]] = []
        self.spilled: list[str] = []

    def key_for(self, stage: str, key_inputs: dict[str, Any]) -> str:
        return hash_json({"stage": stage, "key_inputs": key_inputs})

    def path_for(self, cache_key: str) -> Path:
        return self.objects / cache_key[:2] / f"{cache_key}.json"

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".cas-", suffix=".tmp", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with __import__("os").fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                __import__("os").fsync(handle.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _evict_for(self, needed: int) -> None:
        candidates = sorted(
            self._resident,
            key=lambda key: (self._priorities.get(key, 0), key),
        )
        for key in candidates:
            if self.resident_bytes + needed <= self.max_memory_bytes:
                break
            size = len(self._resident.pop(key))
            self.resident_bytes -= size
            self.evictions.append({"cache_key": key, "bytes": size, "priority": self._priorities.pop(key, 0)})

    def _admit(self, cache_key: str, payload: bytes, priority: int) -> bool:
        size = len(payload)
        self._priorities[cache_key] = priority
        if cache_key in self._resident:
            old = len(self._resident[cache_key])
            self.resident_bytes -= old
        if size <= self.max_memory_bytes and self.resident_bytes + size > self.max_memory_bytes:
            self._evict_for(size)
        if self.resident_bytes + size <= self.max_memory_bytes:
            self._resident[cache_key] = payload
            self.resident_bytes += size
            return True

        self._resident.pop(cache_key, None)
        self._priorities.pop(cache_key, None)
        if size > self.max_memory_bytes and self.overflow_policy == "error":
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "A cache artifact exceeds the resident cache memory limit",
                field="limits.max_cache_memory_bytes",
                details={"artifact_bytes": size, "limit_bytes": self.max_memory_bytes},
            )
        if self.overflow_policy == "error":
            raise WorkbenchError(
                E_RESOURCE_LIMIT,
                "Resident cache memory limit exceeded and eviction was insufficient",
                field="limits.max_cache_memory_bytes",
                details={"needed_bytes": size, "resident_bytes": self.resident_bytes, "limit_bytes": self.max_memory_bytes},
            )
        if cache_key not in self.spilled:
            self.spilled.append(cache_key)
        return False

    def get_or_compute(
        self,
        stage: str,
        key_inputs: dict[str, Any],
        producer: Callable[[], Any],
        *,
        priority: int = 5,
    ) -> tuple[Any, str, bool]:
        cache_key = self.key_for(stage, key_inputs)
        path = self.path_for(cache_key)
        if path.exists():
            payload = path.read_bytes()
            envelope = _loads(payload)
            if envelope.get("cache_key") != cache_key:
                raise WorkbenchError(E_RESOURCE_LIMIT, "Content-addressed cache key mismatch", details={"path": str(path)})
            if envelope.get("payload_checksum") != sha256_bytes(canonical_bytes(envelope.get("payload"))):
                raise WorkbenchError(E_RESOURCE_LIMIT, "Cached artifact checksum is invalid", details={"path": str(path)})
            self.hits += 1
            raw_payload = canonical_bytes(envelope["payload"])
            self._admit(cache_key, raw_payload, priority)
            return envelope["payload"], cache_key, True

        self.misses += 1
        payload_value = producer()
        envelope = {
            "schema": "dedup-workbench/cas-envelope/v1",
            "stage": stage,
            "cache_key": cache_key,
            "key_inputs": key_inputs,
            "payload_checksum": hash_json(payload_value),
            "payload": payload_value,
        }
        payload = canonical_bytes(envelope)
        self._atomic_write(path, payload)
        self._admit(cache_key, canonical_bytes(payload_value), priority)
        return payload_value, cache_key, False

    def stats(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "resident_entries": len(self._resident),
            "resident_bytes": self.resident_bytes,
            "max_memory_bytes": self.max_memory_bytes,
            "eviction_count": len(self.evictions),
            "spilled_count": len(self.spilled),
            "object_count": sum(1 for _ in self.objects.rglob("*.json")),
        }


def _loads(payload: bytes) -> dict[str, Any]:
    import json

    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise WorkbenchError(E_RESOURCE_LIMIT, "Cache envelope must be an object")
    return value
