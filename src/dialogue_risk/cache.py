"""Traceable, version-keyed judgment cache."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .errors import fail
from .util import clone, digest_value


def conversation_content_hash(conversation: dict[str, Any]) -> str:
    content = {
        "conv_id": conversation["conv_id"],
        "locale": conversation["locale"],
        "annotator_group": conversation["annotator_group"],
        "messages": [
            {
                "conv_id": message["conv_id"],
                "msg_no": int(message["msg_no"]),
                "speaker": message["speaker"],
                "text": message["text"],
                "locale": message["locale"],
                "annotator_group": message["annotator_group"],
            }
            for message in sorted(
                conversation["messages"], key=lambda item: int(item["msg_no"])
            )
        ],
    }
    return digest_value(content)


def conversation_weight_digest(conversation: dict[str, Any]) -> str:
    weights = [
        {
            "msg_no": int(message["msg_no"]),
            "weight": float(message.get("weight", 1.0)),
        }
        for message in sorted(
            conversation["messages"], key=lambda item: int(item["msg_no"])
        )
    ]
    return digest_value(weights)


def make_cache_key(
    conversation: dict[str, Any],
    config: dict[str, Any],
    risk_words: dict[str, Any],
) -> str:
    payload = {
        "conv_id": conversation["conv_id"],
        "content_hash": conversation_content_hash(conversation),
        "weight_digest": conversation_weight_digest(conversation),
        "risk_version": int(risk_words["version"]),
        "risk_checksum": digest_value(risk_words),
        "config_version": int(config["config_version"]),
        "config_checksum": digest_value(config),
        "precision_flag": config["precision"]["flag"],
        "precision_decimals": int(config["precision"]["decimals"]),
    }
    return digest_value(payload)


@dataclass
class CacheEntry:
    key: str
    conv_id: str
    content_hash: str
    weight_digest: str
    risk_version: int
    risk_checksum: str
    config_version: int
    config_checksum: str
    precision_flag: str
    built_snapshot_version: int
    result: dict[str, Any]


class JudgmentCache:
    def __init__(
        self,
        enabled: bool,
        max_entries: int,
        resource_limit: int,
    ) -> None:
        self.enabled = enabled
        self.max_entries = min(int(max_entries), int(resource_limit))
        self._entries: dict[str, CacheEntry] = {}
        self._lock = threading.RLock()
        self.stats = {
            "hits": 0,
            "misses": 0,
            "inserts": 0,
            "invalidations": 0,
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def configure(
        self,
        *,
        enabled: bool,
        max_entries: int,
        resource_limit: int,
    ) -> None:
        with self._lock:
            self.enabled = enabled
            self.max_entries = min(int(max_entries), int(resource_limit))

    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def get(self, key: str) -> dict[str, Any] | None:
        if not self.enabled:
            self.stats["misses"] += 1
            return None
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.stats["misses"] += 1
                return None
            self.stats["hits"] += 1
            return clone(entry.result)

    def put(
        self,
        key: str,
        conversation: dict[str, Any],
        config: dict[str, Any],
        risk_words: dict[str, Any],
        snapshot_version: int,
        result: dict[str, Any],
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            if key not in self._entries and len(self._entries) >= self.max_entries:
                fail(
                    "E_RESOURCE_LIMIT",
                    "cache entry limit would be exceeded",
                    degraded_category="cache_entries",
                    current=len(self._entries),
                    limit=self.max_entries,
                    stage="cache",
                )
            self._entries[key] = CacheEntry(
                key=key,
                conv_id=conversation["conv_id"],
                content_hash=conversation_content_hash(conversation),
                weight_digest=conversation_weight_digest(conversation),
                risk_version=int(risk_words["version"]),
                risk_checksum=digest_value(risk_words),
                config_version=int(config["config_version"]),
                config_checksum=digest_value(config),
                precision_flag=config["precision"]["flag"],
                built_snapshot_version=int(snapshot_version),
                result=clone(result),
            )
            self.stats["inserts"] += 1

    def affected_keys(
        self,
        conv_ids: set[str] | None = None,
        all_entries: bool = False,
    ) -> set[str]:
        with self._lock:
            if all_entries:
                return set(self._entries)
            wanted = set(conv_ids or set())
            return {
                key for key, entry in self._entries.items() if entry.conv_id in wanted
            }

    def invalidate_keys(self, keys: set[str]) -> int:
        with self._lock:
            removed = 0
            for key in keys:
                if key in self._entries:
                    del self._entries[key]
                    removed += 1
            self.stats["invalidations"] += removed
            return removed

    def clear(self) -> int:
        with self._lock:
            count = len(self._entries)
            self._entries.clear()
            self.stats["invalidations"] += count
            return count

    def entries(self) -> list[CacheEntry]:
        with self._lock:
            return [clone(entry) for _, entry in sorted(self._entries.items())]

    def stats_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self.stats,
                "entries": len(self._entries),
                "enabled": self.enabled,
                "max_entries": self.max_entries,
            }

