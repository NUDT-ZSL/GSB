"""Content-addressed exact-score cache with explicit resource limits."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import DomainError, E_RESOURCE_LIMIT, E_SNAPSHOT_CORRUPT
from .util import (
    atomic_write_bytes,
    canonical_bytes,
    canonical_json,
    read_json,
    sha256_json,
)


@dataclass(frozen=True)
class CacheEntry:
    cache_key: str
    sample_id: str
    snapshot_id: str
    snapshot_versions: dict[str, int]
    image_content_hash: str
    text_term_digest: str
    precision: str
    feature_digest: str
    score_digest: str
    result: dict[str, Any]
    content_checksum: str
    memory_bytes: int

    @classmethod
    def from_result(cls, result: dict[str, Any]) -> "CacheEntry":
        snapshot_versions = {
            name: int(component["version"])
            for name, component in result["snapshot"]["components"].items()
        }
        base_payload = {
            "schema_version": "igeval.cache-entry/1",
            "cache_key": result["cache_key"],
            "sample_id": result["sample_id"],
            "snapshot_id": result["snapshot"]["snapshot_id"],
            "snapshot_versions": snapshot_versions,
            "image_content_hash": result["input"]["image_content_hash"],
            "text_term_digest": result["input"]["text_term_digest"],
            "precision": result["precision"],
            "feature_digest": result["feature_digest"],
            "score_digest": result["score_digest"],
            "result": result,
        }
        checksum = sha256_json(base_payload)
        memory_bytes = len(canonical_bytes(base_payload))
        return cls(
            cache_key=result["cache_key"],
            sample_id=result["sample_id"],
            snapshot_id=result["snapshot"]["snapshot_id"],
            snapshot_versions=snapshot_versions,
            image_content_hash=result["input"]["image_content_hash"],
            text_term_digest=result["input"]["text_term_digest"],
            precision=result["precision"],
            feature_digest=result["feature_digest"],
            score_digest=result["score_digest"],
            result=result,
            content_checksum=checksum,
            memory_bytes=memory_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "igeval.cache-entry/1",
            "cache_key": self.cache_key,
            "sample_id": self.sample_id,
            "snapshot_id": self.snapshot_id,
            "snapshot_versions": dict(sorted(self.snapshot_versions.items())),
            "image_content_hash": self.image_content_hash,
            "text_term_digest": self.text_term_digest,
            "precision": self.precision,
            "feature_digest": self.feature_digest,
            "score_digest": self.score_digest,
            "result": self.result,
            "content_checksum": self.content_checksum,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CacheEntry":
        if not isinstance(value, dict) or value.get("schema_version") != "igeval.cache-entry/1":
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "invalid cache entry schema",
                category="cache_corrupt",
            )
        checksum = value.get("content_checksum")
        base_payload = {
            key: item for key, item in value.items() if key != "content_checksum"
        }
        expected = sha256_json(base_payload)
        if checksum != expected:
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "cache entry checksum mismatch",
                category="cache_corrupt",
                details={"expected": expected, "actual": checksum},
            )
        result = value["result"]
        if value.get("cache_key") != result.get("cache_key"):
            raise DomainError(
                E_SNAPSHOT_CORRUPT,
                "cache key does not match embedded result",
                category="cache_corrupt",
            )
        return cls(
            cache_key=value["cache_key"],
            sample_id=value["sample_id"],
            snapshot_id=value["snapshot_id"],
            snapshot_versions=dict(value["snapshot_versions"]),
            image_content_hash=value["image_content_hash"],
            text_term_digest=value["text_term_digest"],
            precision=value["precision"],
            feature_digest=value["feature_digest"],
            score_digest=value["score_digest"],
            result=result,
            content_checksum=checksum,
            memory_bytes=len(canonical_bytes(base_payload)),
        )


@dataclass(frozen=True)
class CacheLookup:
    entry: CacheEntry
    source: str
    resident: bool


class CacheStore:
    """Memory cache plus content-addressed disk entries.

    Limits apply to resident in-memory entries. Disk entries remain auditable
    and content-addressed; a disk hit that cannot become resident is still
    returned without increasing resident memory.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        max_entries: int,
        max_memory_bytes: int,
    ) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "cache" / "entries"
        self.entries_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = max_entries
        self.max_memory_bytes = max_memory_bytes
        self._entries: dict[str, CacheEntry] = {}
        self._memory_bytes = 0

    def _path(self, key: str) -> Path:
        return self.entries_dir / key[:2] / f"{key}.json"

    def resident_entries(self) -> int:
        return len(self._entries)

    def resident_memory_bytes(self) -> int:
        return self._memory_bytes

    def stats(self) -> dict[str, Any]:
        return {
            "resident_entries": len(self._entries),
            "resident_feature_memory_bytes": self._memory_bytes,
            "max_cache_entries": self.max_entries,
            "max_feature_memory_bytes": self.max_memory_bytes,
        }

    def _remember(self, entry: CacheEntry) -> bool:
        if entry.cache_key in self._entries:
            return True
        if len(self._entries) + 1 > self.max_entries:
            return False
        if self._memory_bytes + entry.memory_bytes > self.max_memory_bytes:
            return False
        self._entries[entry.cache_key] = entry
        self._memory_bytes += entry.memory_bytes
        return True

    def _load_disk(self, key: str) -> CacheEntry | None:
        path = self._path(key)
        if not path.exists():
            return None
        try:
            payload = read_json(path)
            entry = CacheEntry.from_dict(payload)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, DomainError):
            path.unlink(missing_ok=True)
            return None
        if entry.cache_key != key:
            path.unlink(missing_ok=True)
            return None
        return entry

    def get(self, key: str) -> CacheLookup | None:
        memory_entry = self._entries.get(key)
        if memory_entry is not None:
            return CacheLookup(entry=memory_entry, source="memory", resident=True)
        disk_entry = self._load_disk(key)
        if disk_entry is None:
            return None
        resident = self._remember(disk_entry)
        return CacheLookup(
            entry=disk_entry,
            source="disk",
            resident=resident,
        )

    def put(self, entry: CacheEntry) -> None:
        existing = self._entries.get(entry.cache_key)
        if existing is None:
            if len(self._entries) + 1 > self.max_entries:
                raise DomainError(
                    E_RESOURCE_LIMIT,
                    "resident cache entry limit exceeded",
                    field="max_cache_entries",
                    category="resource",
                    degradation_category="CACHE_ENTRY_LIMIT",
                    details={
                        "limit": self.max_entries,
                        "current": len(self._entries),
                        "requested": 1,
                    },
                )
            if self._memory_bytes + entry.memory_bytes > self.max_memory_bytes:
                raise DomainError(
                    E_RESOURCE_LIMIT,
                    "resident feature memory limit exceeded",
                    field="max_feature_memory_bytes",
                    category="resource",
                    degradation_category="FEATURE_MEMORY_LIMIT",
                    details={
                        "limit": self.max_memory_bytes,
                        "current": self._memory_bytes,
                        "requested": entry.memory_bytes,
                    },
                )
        path = self._path(entry.cache_key)
        try:
            atomic_write_bytes(path, canonical_bytes(entry.to_dict()))
        except OSError as exc:
            raise DomainError(
                E_RESOURCE_LIMIT,
                f"cannot persist content-addressed cache entry: {exc}",
                field="cache_directory",
                category="resource",
                degradation_category="DISK_CACHE_IO",
            ) from exc
        if existing is None:
            self._entries[entry.cache_key] = entry
            self._memory_bytes += entry.memory_bytes
        elif existing != entry:
            self._memory_bytes -= existing.memory_bytes
            self._entries[entry.cache_key] = entry
            self._memory_bytes += entry.memory_bytes

    def peek(self, key: str) -> CacheEntry | None:
        entry = self._entries.get(key)
        return entry if entry is not None else self._load_disk(key)

    def invalidate_keys(self, keys: list[str]) -> list[str]:
        invalidated: list[str] = []
        for key in sorted(set(keys)):
            entry = self._entries.pop(key, None)
            if entry is not None:
                self._memory_bytes -= entry.memory_bytes
                invalidated.append(key)
            path = self._path(key)
            if path.exists():
                path.unlink(missing_ok=True)
                if key not in invalidated:
                    invalidated.append(key)
        return invalidated

    def all_keys(self) -> list[str]:
        keys: set[str] = set(self._entries)
        if self.entries_dir.exists():
            for path in self.entries_dir.rglob("*.json"):
                if len(path.stem) == 64:
                    keys.add(path.stem)
        return sorted(keys)

    def keys_for_sample(self, sample_id: str) -> list[str]:
        keys: set[str] = {
            key
            for key, entry in self._entries.items()
            if entry.sample_id == sample_id
        }
        if self.entries_dir.exists():
            for path in self.entries_dir.rglob("*.json"):
                if len(path.stem) != 64:
                    continue
                try:
                    payload = read_json(path)
                    if payload.get("sample_id") == sample_id:
                        keys.add(path.stem)
                except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
                    continue
        return sorted(keys)

    def clear(self) -> None:
        self._entries.clear()
        self._memory_bytes = 0
        if self.entries_dir.exists():
            for path in self.entries_dir.rglob("*.json"):
                path.unlink(missing_ok=True)

    def describe_entry(self, key: str) -> dict[str, Any] | None:
        entry = self.peek(key)
        if entry is None:
            return None
        return {
            "cache_key": entry.cache_key,
            "sample_id": entry.sample_id,
            "snapshot_id": entry.snapshot_id,
            "snapshot_versions": dict(sorted(entry.snapshot_versions.items())),
            "image_content_hash": entry.image_content_hash,
            "text_term_digest": entry.text_term_digest,
            "precision": entry.precision,
            "feature_digest": entry.feature_digest,
            "score_digest": entry.score_digest,
            "content_checksum": entry.content_checksum,
            "memory_bytes": entry.memory_bytes,
            "disk_path": str(self._path(key)),
        }

    def verify_entry_matches(self, entry: CacheEntry, result: dict[str, Any]) -> bool:
        return (
            entry.cache_key == result["cache_key"]
            and entry.feature_digest == result["feature_digest"]
            and entry.score_digest == result["score_digest"]
            and entry.snapshot_id == result["snapshot"]["snapshot_id"]
            and entry.image_content_hash == result["input"]["image_content_hash"]
            and entry.text_term_digest == result["input"]["text_term_digest"]
            and entry.precision == result["precision"]
        )

    def canonical_entry(self, key: str) -> str | None:
        entry = self.peek(key)
        if entry is None:
            return None
        return canonical_json(entry.to_dict())

