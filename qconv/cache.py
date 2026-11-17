"""Content-addressed artifact cache with a deterministic residency budget."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .util import array_digest


KEY_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass
class CacheArtifact:
    cache_key: str
    node_id: str
    integer: np.ndarray
    output: np.ndarray
    integer_digest: str
    output_digest: str
    metadata: dict[str, Any]
    error_summary: dict[str, Any]
    macs_executed: int
    macs_expected: int
    size_bytes: int = 0


@dataclass
class CacheStoreResult:
    stored: bool
    size_bytes: int
    evicted: list[str] = field(default_factory=list)


class ArtifactCache:
    def __init__(self, directory: str | Path, budget_bytes: int) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.budget_bytes = max(0, int(budget_bytes))
        self.evictions: list[str] = []
        self.hits: list[str] = []
        self.misses: list[str] = []

    def _path(self, cache_key: str) -> Path:
        if not KEY_RE.fullmatch(cache_key):
            raise ValueError(f"invalid cache key: {cache_key!r}")
        return self.directory / f"{cache_key}.qcache.npz"

    def existing_keys(self) -> list[str]:
        keys = []
        for path in self.directory.glob("*.qcache.npz"):
            key = path.name.removesuffix(".qcache.npz")
            if KEY_RE.fullmatch(key):
                keys.append(key)
        return sorted(keys)

    def used_bytes(self) -> int:
        return sum(self._path(key).stat().st_size for key in self.existing_keys() if self._path(key).exists())

    def clear(self) -> list[str]:
        removed: list[str] = []
        for key in self.existing_keys():
            try:
                self._path(key).unlink()
                removed.append(key)
            except FileNotFoundError:
                pass
        return removed

    def evict_to_budget(self) -> None:
        if self.budget_bytes <= 0:
            return
        current = self.used_bytes()
        for key in self.existing_keys():
            if current <= self.budget_bytes:
                break
            path = self._path(key)
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                continue
            current -= size
            self.evictions.append(key)

    def load(self, cache_key: str) -> CacheArtifact | None:
        path = self._path(cache_key)
        if not path.exists():
            self.misses.append(cache_key)
            return None
        try:
            with np.load(path, allow_pickle=False) as archive:
                integer = np.asarray(archive["integer"], dtype=np.int64)
                output = np.asarray(archive["output"], dtype=np.float64)
                meta_raw = str(archive["meta"].item())
            metadata = json.loads(meta_raw)
            if metadata.get("cache_key") != cache_key:
                raise ValueError("cache key metadata mismatch")
            integer_digest = array_digest(integer)
            output_digest = array_digest(output)
            if integer_digest != metadata.get("integer_digest"):
                raise ValueError("cached integer digest mismatch")
            if output_digest != metadata.get("output_digest"):
                raise ValueError("cached output digest mismatch")
        except (OSError, ValueError, KeyError, json.JSONDecodeError, TypeError):
            self.misses.append(cache_key)
            return None
        self.hits.append(cache_key)
        return CacheArtifact(
            cache_key=cache_key,
            node_id=str(metadata["node_id"]),
            integer=integer,
            output=output,
            integer_digest=integer_digest,
            output_digest=output_digest,
            metadata=dict(metadata.get("conv_metadata", {})),
            error_summary=dict(metadata.get("error_summary", {})),
            macs_executed=int(metadata.get("macs_executed", 0)),
            macs_expected=int(metadata.get("macs_expected", 0)),
            size_bytes=path.stat().st_size,
        )

    def store(self, artifact: CacheArtifact) -> CacheStoreResult:
        if self.budget_bytes <= 0:
            return CacheStoreResult(stored=False, size_bytes=0)
        path = self._path(artifact.cache_key)
        metadata = {
            "cache_key": artifact.cache_key,
            "node_id": artifact.node_id,
            "integer_digest": artifact.integer_digest,
            "output_digest": artifact.output_digest,
            "macs_executed": artifact.macs_executed,
            "macs_expected": artifact.macs_expected,
            "conv_metadata": artifact.metadata,
            "error_summary": artifact.error_summary,
        }
        fd, temp_name = tempfile.mkstemp(prefix=".qcache-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as handle:
                np.savez_compressed(
                    handle,
                    integer=np.asarray(artifact.integer, dtype=np.int64),
                    output=np.asarray(artifact.output, dtype=np.float64),
                    meta=np.asarray(json.dumps(metadata, sort_keys=True, ensure_ascii=False)),
                )
                handle.flush()
                os.fsync(handle.fileno())
            new_size = os.path.getsize(temp_name)
            if new_size > self.budget_bytes:
                os.unlink(temp_name)
                return CacheStoreResult(stored=False, size_bytes=new_size)
            existing_size = path.stat().st_size if path.exists() else 0
            current = self.used_bytes() - existing_size
            evicted: list[str] = []
            for key in self.existing_keys():
                if key == artifact.cache_key:
                    continue
                if current + new_size <= self.budget_bytes:
                    break
                candidate = self._path(key)
                try:
                    size = candidate.stat().st_size
                    candidate.unlink()
                except FileNotFoundError:
                    continue
                current -= size
                evicted.append(key)
                self.evictions.append(key)
            if current + new_size > self.budget_bytes:
                os.unlink(temp_name)
                return CacheStoreResult(stored=False, size_bytes=new_size, evicted=evicted)
            os.replace(temp_name, path)
            return CacheStoreResult(stored=True, size_bytes=new_size, evicted=evicted)
        except BaseException:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
            raise

    def report(self) -> dict[str, Any]:
        return {
            "budget_bytes": self.budget_bytes,
            "used_bytes": self.used_bytes(),
            "artifact_count": len(self.existing_keys()),
            "hits": list(self.hits),
            "misses": list(self.misses),
            "evictions": list(self.evictions),
        }
