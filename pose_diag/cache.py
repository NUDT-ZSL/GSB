"""Stage-aware deterministic result cache."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass
from typing import Any

from .io_utils import sha256_json


def make_cache_key(stage: str, *parts: Any) -> str:
    return f"{stage}:{sha256_json({'stage': stage, 'parts': list(parts)})}"


@dataclass
class CacheNode:
    key: str
    stage: str
    value: Any
    metadata: dict[str, Any]


class PipelineCache:
    def __init__(self) -> None:
        self._nodes: dict[str, CacheNode] = {}
        self._stats: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    def get(self, key: str, stage: str) -> Any | None:
        with self._lock:
            node = self._nodes.get(key)
            stats = self._stats.setdefault(stage, {"hits": 0, "misses": 0})
            if node is None:
                stats["misses"] += 1
                return None
            stats["hits"] += 1
            return copy.deepcopy(node.value)

    def get_node(self, key: str) -> CacheNode | None:
        with self._lock:
            node = self._nodes.get(key)
            return copy.deepcopy(node) if node is not None else None

    def get_value_exact(self, key: str) -> Any | None:
        with self._lock:
            node = self._nodes.get(key)
            return copy.deepcopy(node.value) if node is not None else None

    def put(
        self,
        key: str,
        stage: str,
        value: Any,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._nodes[key] = CacheNode(
                key=key,
                stage=stage,
                value=copy.deepcopy(value),
                metadata=dict(metadata or {}),
            )

    def stats(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return copy.deepcopy(self._stats)

    def delta(
        self, before: dict[str, dict[str, int]]
    ) -> dict[str, dict[str, int]]:
        after = self.stats()
        delta: dict[str, dict[str, int]] = {}
        for stage in sorted(set(before) | set(after)):
            old = before.get(stage, {"hits": 0, "misses": 0})
            new = after.get(stage, {"hits": 0, "misses": 0})
            delta[stage] = {
                "hits": new["hits"] - old["hits"],
                "misses": new["misses"] - old["misses"],
            }
        return delta

