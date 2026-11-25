from __future__ import annotations

import json
import os
import tempfile
from typing import Any

import numpy as np

from .hashing import content_hash


def atomic_write_json(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


class CacheStore:
    """Content-addressed stage artifact store with cache-key index aliases."""

    def __init__(self, root: str):
        self.root = root
        self.objects = os.path.join(root, "objects")
        self.index_path = os.path.join(root, "index.json")
        os.makedirs(self.objects, exist_ok=True)
        self.index = self._load_index()

    def _load_index(self) -> dict[str, Any]:
        if os.path.exists(self.index_path):
            with open(self.index_path, "r", encoding="utf-8") as handle:
                return json.load(handle)
        return {"version": 1, "keys": {}}

    def _save_index(self) -> None:
        atomic_write_json(self.index_path, self.index)

    def has_key(self, cache_key: str) -> bool:
        return cache_key in self.index["keys"]

    def get_key(self, cache_key: str) -> dict[str, Any] | None:
        return self.index["keys"].get(cache_key)

    def object_json_path(self, artifact_hash: str) -> str:
        return os.path.join(self.objects, f"{artifact_hash}.json")

    def object_npz_path(self, artifact_hash: str) -> str:
        return os.path.join(self.objects, f"{artifact_hash}.npz")

    def put(
        self,
        stage: str,
        cache_key: str,
        payload: dict[str, Any],
        arrays: dict[str, np.ndarray] | None = None,
        key_inputs: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_hash = content_hash({"stage": stage, "payload": payload}, arrays)
        record = {
            "stage": stage,
            "cache_key": cache_key,
            "artifact_hash": artifact_hash,
            "key_inputs": key_inputs or {},
            "has_arrays": bool(arrays),
        }
        full_payload = {"record": record, "payload": payload}
        atomic_write_json(self.object_json_path(artifact_hash), full_payload)
        if arrays:
            np.savez_compressed(self.object_npz_path(artifact_hash), **arrays)
        self.index["keys"][cache_key] = record
        self._save_index()
        return record

    def link_key(self, cache_key: str, artifact_hash: str, key_inputs: dict[str, Any] | None = None) -> dict[str, Any]:
        source_path = self.object_json_path(artifact_hash)
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"cannot link missing artifact {artifact_hash}")
        with open(source_path, "r", encoding="utf-8") as handle:
            full = json.load(handle)
        record = dict(full["record"])
        record["cache_key"] = cache_key
        record["linked_from_hash"] = artifact_hash
        if key_inputs is not None:
            record["key_inputs"] = key_inputs
        self.index["keys"][cache_key] = record
        self._save_index()
        return record

    def load_payload(self, artifact_hash: str) -> dict[str, Any]:
        with open(self.object_json_path(artifact_hash), "r", encoding="utf-8") as handle:
            return json.load(handle)

    def load_arrays(self, artifact_hash: str) -> dict[str, np.ndarray]:
        path = self.object_npz_path(artifact_hash)
        if not os.path.exists(path):
            return {}
        with np.load(path, allow_pickle=False) as data:
            return {key: data[key] for key in data.files}
