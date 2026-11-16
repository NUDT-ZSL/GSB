"""Thread-safe caches with explicit key mismatch invalidation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    invalidations: int = 0
    computes: int = 0
    bypasses: int = 0
    compute_counts: dict[str, int] = field(default_factory=dict)
    invalidated_namespaces: set[str] = field(default_factory=set)
    invalidation_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "invalidations": self.invalidations,
            "computes": self.computes,
            "bypasses": self.bypasses,
            "compute_counts": dict(sorted(self.compute_counts.items())),
            "invalidated_namespaces": sorted(self.invalidated_namespaces),
            "invalidation_counts": dict(sorted(self.invalidation_counts.items())),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "CacheStats":
        return cls(
            hits=int(raw.get("hits", 0)),
            misses=int(raw.get("misses", 0)),
            invalidations=int(raw.get("invalidations", 0)),
            computes=int(raw.get("computes", 0)),
            bypasses=int(raw.get("bypasses", 0)),
            compute_counts={
                str(key): int(value)
                for key, value in dict(raw.get("compute_counts", {})).items()
            },
            invalidated_namespaces=set(
                str(value) for value in raw.get("invalidated_namespaces", [])
            ),
            invalidation_counts={
                str(key): int(value)
                for key, value in dict(raw.get("invalidation_counts", {})).items()
            },
        )


@dataclass
class _Entry:
    key: str
    value: Any


class KeyedCache:
    """A small verified cache.

    Entries are addressed by a semantic namespace (for example column + CV fold).
    A different key in the same namespace is not silently reused: it is counted as
    an invalidation and replaced only after a fresh computation.
    """

    def __init__(
        self,
        *,
        encode: Callable[[Any], Any],
        decode: Callable[[Any], Any],
        enabled: bool = True,
    ) -> None:
        self._encode = encode
        self._decode = decode
        self.enabled = enabled
        self._entries: dict[str, _Entry] = {}
        self.stats = CacheStats()
        self._lock = threading.RLock()

    def get_or_compute(
        self,
        namespace: str,
        key: str,
        factory: Callable[[], Any],
    ) -> Any:
        with self._lock:
            if not self.enabled:
                self.stats.bypasses += 1
                self.stats.computes += 1
                self.stats.compute_counts[namespace] = (
                    self.stats.compute_counts.get(namespace, 0) + 1
                )
                return factory()
            entry = self._entries.get(namespace)
            if entry is not None and entry.key == key:
                self.stats.hits += 1
                return entry.value
            if entry is not None:
                self.stats.invalidations += 1
                self.stats.invalidated_namespaces.add(namespace)
                self.stats.invalidation_counts[namespace] = (
                    self.stats.invalidation_counts.get(namespace, 0) + 1
                )
            else:
                self.stats.misses += 1
            self.stats.computes += 1
            self.stats.compute_counts[namespace] = (
                self.stats.compute_counts.get(namespace, 0) + 1
            )
            value = factory()
            self._entries[namespace] = _Entry(key=key, value=value)
            return value

    def get_or_compute_validated(
        self,
        namespace: str,
        key: str,
        factory: Callable[[], Any],
        validator: Callable[[Any], bool],
    ) -> Any:
        """Like get_or_compute, but validate the decoded value before reuse."""

        with self._lock:
            if not self.enabled:
                self.stats.bypasses += 1
                self.stats.computes += 1
                self.stats.compute_counts[namespace] = (
                    self.stats.compute_counts.get(namespace, 0) + 1
                )
                return factory()
            entry = self._entries.get(namespace)
            invalid = False
            if entry is not None and entry.key == key:
                try:
                    valid = bool(validator(entry.value))
                except Exception:
                    valid = False
                if valid:
                    self.stats.hits += 1
                    return entry.value
                invalid = True
            elif entry is not None:
                invalid = True
            if invalid:
                self.stats.invalidations += 1
                self.stats.invalidated_namespaces.add(namespace)
                self.stats.invalidation_counts[namespace] = (
                    self.stats.invalidation_counts.get(namespace, 0) + 1
                )
            else:
                self.stats.misses += 1
            self.stats.computes += 1
            self.stats.compute_counts[namespace] = (
                self.stats.compute_counts.get(namespace, 0) + 1
            )
            value = factory()
            self._entries[namespace] = _Entry(key=key, value=value)
            return value

    def delete(self, namespace: str) -> None:
        with self._lock:
            self._entries.pop(namespace, None)

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.enabled,
                "stats": self.stats.to_dict(),
                "entries": [
                    {
                        "namespace": namespace,
                        "key": entry.key,
                        "value": self._encode(entry.value),
                    }
                    for namespace, entry in sorted(self._entries.items())
                ],
            }

    def load_dict(self, raw: Mapping[str, Any]) -> None:
        with self._lock:
            self.enabled = bool(raw.get("enabled", True))
            self.stats = CacheStats.from_dict(raw.get("stats", {}))
            entries: dict[str, _Entry] = {}
            for item in raw.get("entries", []):
                namespace = str(item["namespace"])
                entries[namespace] = _Entry(
                    key=str(item["key"]), value=self._decode(item["value"])
                )
            self._entries = entries

    def stats_dict(self) -> dict[str, Any]:
        with self._lock:
            return self.stats.to_dict()

    def invalidation_counts(self) -> dict[str, int]:
        with self._lock:
            return dict(self.stats.invalidation_counts)


class CacheBundle:
    def __init__(self, *, enabled: bool = True) -> None:
        from .encoding import FeatureArtifact
        from .selector import SelectorState

        self.features = KeyedCache(
            encode=lambda artifact: artifact.to_cache_dict(),
            decode=FeatureArtifact.from_cache_dict,
            enabled=enabled,
        )
        self.selectors = KeyedCache(
            encode=lambda state: state.to_dict(),
            decode=SelectorState.from_dict,
            enabled=enabled,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "features": self.features.to_dict(),
            "selectors": self.selectors.to_dict(),
        }

    def load_dict(self, raw: Mapping[str, Any]) -> None:
        self.features.load_dict(raw.get("features", {}))
        self.selectors.load_dict(raw.get("selectors", {}))

    def summary(self) -> dict[str, Any]:
        features = self.features.stats_dict()
        selectors = self.selectors.stats_dict()
        return {
            "hits": features["hits"] + selectors["hits"],
            "misses": features["misses"] + selectors["misses"],
            "invalidations": features["invalidations"] + selectors["invalidations"],
            "computes": features["computes"] + selectors["computes"],
            "bypasses": features["bypasses"] + selectors["bypasses"],
            "invalidated_feature_columns": sorted(
                {
                    namespace.split(":", 2)[1]
                    for namespace in features["invalidated_namespaces"]
                    if namespace.startswith("feature:") and ":" in namespace
                }
            ),
            "invalidated_feature_count": len(
                {
                    namespace.split(":", 2)[1]
                    for namespace in features["invalidated_namespaces"]
                    if namespace.startswith("feature:") and ":" in namespace
                }
            ),
            "feature": features,
            "selector": selectors,
            "feature_invalidation_counts": self.features.invalidation_counts(),
        }

    def delta(self, before: Mapping[str, Any]) -> dict[str, Any]:
        after = self.summary()
        return {
            "hits": after["hits"] - int(before.get("hits", 0)),
            "misses": after["misses"] - int(before.get("misses", 0)),
            "invalidations": after["invalidations"] - int(before.get("invalidations", 0)),
            "computes": after["computes"] - int(before.get("computes", 0)),
            "bypasses": after["bypasses"] - int(before.get("bypasses", 0)),
            "invalidated_feature_columns": after["invalidated_feature_columns"],
        }
