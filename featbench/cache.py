"""Content addressed stage cache with derived keys and reuse/invalidation bookkeeping."""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

from .atomicio import atomic_write_pickle, read_pickle
from .hashing import digest_iterable, fingerprint

STAGES = ("cross", "encode", "reduce", "evaluate")


@dataclass
class CacheLedger:
    reused: list[str] = field(default_factory=list)
    recomputed: list[str] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)
    hits: int = 0
    misses: int = 0

    def record_reuse(self, key: str) -> None:
        self.reused.append(key)
        self.hits += 1

    def record_recompute(self, key: str) -> None:
        self.recomputed.append(key)
        self.misses += 1

    def record_invalidation(self, key: str) -> None:
        self.invalidated.append(key)

    def as_dict(self) -> dict:
        return {
            "reused": sorted(self.reused),
            "recomputed": sorted(self.recomputed),
            "invalidated": sorted(self.invalidated),
            "hit_count": self.hits,
            "miss_count": self.misses,
        }


class CacheStore:
    """Stores stage artifacts under work_dir/cache keyed by derived fingerprints."""

    def __init__(self, work_dir: str, config_fingerprint: str):
        # Namespace includes the absolute work_dir so two separate experiments
        # with identical content fingerprints never share cached artifacts.
        namespace = fingerprint({"work_dir": os.path.abspath(work_dir),
                                 "config": config_fingerprint},
                                prefix="namespace:")
        self.root = os.path.join(work_dir, "cache", namespace)
        self.config_fingerprint = config_fingerprint
        os.makedirs(self.root, exist_ok=True)
        self.index_path = os.path.join(self.root, "index.json")
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}

    def lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._key_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[key] = lock
            return lock

    def stage_key(self, stage: str, scale_id: str, path_name: str,
                  upstream: list[str], params: dict) -> str:
        material = {
            "stage": stage,
            "scale_id": scale_id,
            "path": path_name,
            "upstream": upstream,
            "params": params,
            "config": self.config_fingerprint,
        }
        return f"{stage}__{scale_id}__{path_name}__" \
               f"{fingerprint(material, prefix='key:')[:16]}"

    def path_for(self, key: str) -> str:
        return os.path.join(self.root, f"{key}.pkl")

    def has(self, key: str) -> bool:
        return os.path.isfile(self.path_for(key))

    def load(self, key: str):
        return read_pickle(self.path_for(key))

    def store(self, key: str, artifact) -> None:
        atomic_write_pickle(self.path_for(key), artifact)

    def prune_orphans(self, live_keys: set[str]) -> list[str]:
        """Invalidate cached artifacts that are no longer reachable."""
        invalidated: list[str] = []
        for filename in os.listdir(self.root):
            if not filename.endswith(".pkl"):
                continue
            key = filename[:-4]
            if key not in live_keys:
                os.unlink(os.path.join(self.root, filename))
                invalidated.append(key)
        return invalidated


def chain_key(store: CacheStore, stage: str, scale_id: str, path_name: str,
              upstream_fingerprints: list[str], params: dict) -> str:
    return store.stage_key(stage, scale_id, path_name,
                           [digest_iterable(upstream_fingerprints)], params)
