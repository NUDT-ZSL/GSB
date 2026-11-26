"""Deterministic JSON file cache and dependency-aware cache keys."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .canonical import content_hash, read_json, write_json
from .evidence import ALGORITHM_VERSION, CACHE_KEY_VERSION


def dependency_hash(mentions: list[dict[str, Any]], rule_book: dict[str, Any]) -> str:
    mention_fingerprint = [
        {
            "mention_id": item["mention_id"],
            "doc_id": item["doc_id"],
            "sent_id": item["sent_id"],
            "start": item["start"],
            "end": item["end"],
            "name": item["name"],
            "text": item["text"],
            "entity_type": item["entity_type"],
            "global_start": item["global_start"],
            "global_end": item["global_end"],
        }
        for item in sorted(mentions, key=lambda item: item["mention_id"])
    ]
    return content_hash(
        {
            "cache_key_version": CACHE_KEY_VERSION,
            "algorithm_version": ALGORITHM_VERSION,
            "mentions": mention_fingerprint,
            "rule_book": rule_book,
        }
    )


class CacheStore:
    def __init__(self, cache_dir: str | Path) -> None:
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        safe = key.replace(":", "_").replace("/", "_")
        return self.dir / f"{safe}.json"

    def get(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        self.hits += 1
        return read_json(path)

    def put(self, key: str, payload: dict[str, Any]) -> Path:
        return write_json(self._path(key), {"key": key, "payload": payload})

    def get_current(self, key: str, current_dependency_hash: str) -> dict[str, Any] | None:
        entry = self.get(key)
        if entry is None:
            return None
        if entry.get("dependency_hash") != current_dependency_hash:
            return None
        return entry.get("payload")

    def put_current(self, key: str, dependency_hash: str, payload: dict[str, Any]) -> Path:
        return write_json(
            self._path(key),
            {"key": key, "dependency_hash": dependency_hash, "payload": payload},
        )


def closure_cache_key(chain: dict[str, Any], dependency_hash_value: str) -> str:
    return f"closure-{CACHE_KEY_VERSION}-{dependency_hash_value[:12]}-{chain['chain_id']}"
