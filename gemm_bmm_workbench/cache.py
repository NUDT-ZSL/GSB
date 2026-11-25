from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .canonical import atomic_write_json, read_json, sha256_json, write_npy_atomic


CACHE_SCHEMA = "content-cache/v1"


def make_node_key(node: str, key_parts: dict[str, Any]) -> str:
    return sha256_json({"cache_schema": CACHE_SCHEMA, "node": node, "parts": key_parts})


class ContentCache:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.objects = self.root / "objects"
        self.blobs = self.root / "blobs"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.blobs.mkdir(parents=True, exist_ok=True)

    def object_path(self, key: str) -> Path:
        return self.objects / key[:2] / f"{key}.json"

    def blob_path(self, digest: str) -> Path:
        return self.blobs / f"{digest}.npy"

    def has_object(self, key: str) -> bool:
        return self.object_path(key).is_file()

    def get_object(self, key: str) -> dict[str, Any]:
        return read_json(self.object_path(key))

    def put_object(self, key: str, payload: dict[str, Any]) -> None:
        record = {"key": key, "payload": payload}
        atomic_write_json(self.object_path(key), record)

    def put_blob(self, digest: str, array: np.ndarray) -> Path:
        path = self.blob_path(digest)
        if not path.exists():
            write_npy_atomic(path, array)
        return path

    def load_blob(self, digest: str) -> np.ndarray:
        return np.load(self.blob_path(digest), allow_pickle=False)


class StagedCacheCommit:
    """Pending cache object/blob writes promoted only with a stage commit."""

    def __init__(self) -> None:
        self.objects: list[tuple[str, dict[str, Any]]] = []
        self.blobs: list[tuple[str, Path]] = []

    def add_object(self, key: str, payload: dict[str, Any]) -> None:
        self.objects.append((key, payload))

    def add_blob(self, digest: str, source_path: Path) -> None:
        self.blobs.append((digest, source_path))

    def promote(self, cache: ContentCache) -> None:
        for digest, source_path in self.blobs:
            target = cache.blob_path(digest)
            target.parent.mkdir(parents=True, exist_ok=True)
            if source_path != target and not target.exists():
                source_path.replace(target)
        for key, payload in self.objects:
            cache.put_object(key, payload)


def cache_key_report(node: str, key: str, key_parts: dict[str, Any], hit: bool) -> dict[str, Any]:
    return {
        "node": node,
        "key": key,
        "hit": bool(hit),
        "key_composition": key_parts,
        "key_formula": "sha256(canonical_json({cache_schema,node,parts}))",
    }


def verify_key_composition(record: dict[str, Any]) -> bool:
    return make_node_key(record["node"], record["key_composition"]) == record["key"]
