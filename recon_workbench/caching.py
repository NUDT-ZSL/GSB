"""Content-addressed artifact cache with dependency fingerprints."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from .canonical import fingerprint, read_json, sha256_json, atomic_write_json


CACHE_SCHEMA_VERSION = 1
IMPLEMENTATION_VERSION = "recon-workbench-1.0.0"


def feature_cache_key(
    *,
    image_hash: str,
    camera_hash: str,
    feature_params: dict[str, Any],
    seed: int,
    view_index: int,
    injection_hash: str,
) -> str:
    return fingerprint(
        "feature-cache-v1",
        {
            "implementation": IMPLEMENTATION_VERSION,
            "image_hash": image_hash,
            "camera_hash": camera_hash,
            "feature_params": feature_params,
            "seed": seed,
            "view_index": view_index,
            "injection_hash": injection_hash,
        },
    )


def pair_cache_key(
    *,
    pair_id: str,
    feature_keys: list[str],
    camera_hashes: list[str],
    matching_params: dict[str, Any],
    triangulation_params: dict[str, Any],
    injection_hash: str,
) -> str:
    return fingerprint(
        "pair-cache-v1",
        {
            "implementation": IMPLEMENTATION_VERSION,
            "pair_id": pair_id,
            "feature_keys": feature_keys,
            "camera_hashes": camera_hashes,
            "matching_params": matching_params,
            "triangulation_params": triangulation_params,
            "injection_hash": injection_hash,
        },
    )


def depth_cache_key(
    *,
    view_index: int,
    camera_hash: str,
    contributing_pair_keys: list[str],
    depth_params: dict[str, Any],
    triangulation_params: dict[str, Any],
    image_size: tuple[int, int],
    injection_hash: str,
) -> str:
    return fingerprint(
        "depth-cache-v1",
        {
            "implementation": IMPLEMENTATION_VERSION,
            "view_index": view_index,
            "camera_hash": camera_hash,
            "contributing_pair_keys": contributing_pair_keys,
            "depth_params": depth_params,
            "triangulation_params": triangulation_params,
            "image_size": list(image_size),
            "injection_hash": injection_hash,
        },
    )


def cloud_cache_key(
    *,
    pair_keys: list[str],
    depth_keys: list[str],
    pointcloud_params: dict[str, Any],
    injection_hash: str,
) -> str:
    return fingerprint(
        "cloud-cache-v1",
        {
            "implementation": IMPLEMENTATION_VERSION,
            "pair_keys": pair_keys,
            "depth_keys": depth_keys,
            "pointcloud_params": pointcloud_params,
            "injection_hash": injection_hash,
        },
    )


class ArtifactStore:
    def __init__(self, root: Path, *, enabled: bool = True):
        self.root = Path(root)
        self.enabled = enabled
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stats: dict[str, dict[str, Any]] = {
            kind: {"reused": 0, "recomputed": 0, "invalid": 0, "reused_keys": [], "recomputed_keys": []}
            for kind in ("features", "pairs", "depth", "cloud")
        }

    def path_for(self, kind: str, key: str, *parts: str) -> Path:
        return self.root / kind / Path(*parts) / f"{key}.json"

    def load(self, kind: str, key: str, path: Path) -> Any | None:
        if not self.enabled or not path.exists():
            return None
        try:
            wrapper = read_json(path)
            if wrapper.get("cache_schema_version") != CACHE_SCHEMA_VERSION:
                raise ValueError("cache schema mismatch")
            if wrapper.get("kind") != kind or wrapper.get("key") != key:
                raise ValueError("cache key mismatch")
            payload = wrapper.get("payload")
            if sha256_json(payload) != wrapper.get("payload_hash"):
                raise ValueError("cache payload hash mismatch")
        except Exception:
            with self._lock:
                self._stats[kind]["invalid"] += 1
            return None
        with self._lock:
            self._stats[kind]["reused"] += 1
            self._stats[kind]["reused_keys"].append(key)
        return payload

    def save(self, kind: str, key: str, path: Path, payload: Any) -> None:
        if not self.enabled:
            with self._lock:
                self._stats[kind]["recomputed"] += 1
                self._stats[kind]["recomputed_keys"].append(key)
            return
        wrapper = {
            "cache_schema_version": CACHE_SCHEMA_VERSION,
            "kind": kind,
            "key": key,
            "payload_hash": sha256_json(payload),
            "payload": payload,
        }
        atomic_write_json(path, wrapper)
        with self._lock:
            self._stats[kind]["recomputed"] += 1
            self._stats[kind]["recomputed_keys"].append(key)

    def report(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {}
            for kind, stats in self._stats.items():
                result[kind] = {
                    "reused": stats["reused"],
                    "recomputed": stats["recomputed"],
                    "invalid": stats["invalid"],
                    "reused_keys": sorted(set(stats["reused_keys"])),
                    "recomputed_keys": sorted(set(stats["recomputed_keys"])),
                }
            return result
