"""Content-addressable artifact cache.

Every DAG node artifact is stored under a key that hashes the full
canonical payload: node name, job manifest, input-tensor content hash,
quant_scale / zero_point, acc_dtype, block_size and the upstream node
artifact hashes. Identical key -> reuse; different key -> recompute.
Each entry carries its payload so it can be replayed independently.
"""
from __future__ import annotations

import hashlib
import io
import json
import os

import numpy as np


def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_array(arr: np.ndarray) -> str:
    arr = np.ascontiguousarray(arr)
    h = hashlib.sha256()
    h.update(str(arr.dtype).encode())
    h.update(json.dumps(list(arr.shape)).encode())
    h.update(arr.tobytes())
    return h.hexdigest()


def serialize_artifact(artifact: dict) -> bytes:
    """dict[str, ndarray] -> deterministic bytes (npy frames, sorted)."""
    buf = io.BytesIO()
    for name in sorted(artifact):
        frame = io.BytesIO()
        np.save(frame, artifact[name], allow_pickle=False)
        data = frame.getvalue()
        buf.write(len(name).to_bytes(2, "big"))
        buf.write(name.encode())
        buf.write(len(data).to_bytes(8, "big"))
        buf.write(data)
    return buf.getvalue()


def deserialize_artifact(data: bytes) -> dict:
    buf = io.BytesIO(data)
    out = {}
    while True:
        raw = buf.read(2)
        if not raw:
            break
        name = buf.read(int.from_bytes(raw, "big")).decode()
        size = int.from_bytes(buf.read(8), "big")
        out[name] = np.load(io.BytesIO(buf.read(size)), allow_pickle=False)
    return out


class ContentCache:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    @staticmethod
    def key_for(node: str, payload: dict) -> str:
        return hash_bytes(canonical_json({"node": node,
                                          "payload": payload}).encode())

    def _paths(self, key):
        sub = os.path.join(self.root, key[:2])
        return (os.path.join(sub, key + ".bin"),
                os.path.join(sub, key + ".json"))

    def has(self, key) -> bool:
        bin_path, meta_path = self._paths(key)
        return os.path.exists(bin_path) and os.path.exists(meta_path)

    def put(self, key, artifact: dict, meta: dict) -> str:
        bin_path, meta_path = self._paths(key)
        os.makedirs(os.path.dirname(bin_path), exist_ok=True)
        blob = serialize_artifact(artifact)
        meta = dict(meta)
        meta["artifact_sha256"] = hash_bytes(blob)
        tmp = bin_path + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, bin_path)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2, sort_keys=True)
        return key

    def get(self, key):
        bin_path, meta_path = self._paths(key)
        if not (os.path.exists(bin_path) and os.path.exists(meta_path)):
            return None
        with open(bin_path, "rb") as fh:
            blob = fh.read()
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        if hash_bytes(blob) != meta.get("artifact_sha256"):
            raise ValueError(f"cache entry corrupt: {key}")
        return deserialize_artifact(blob), meta

    def meta(self, key):
        _, meta_path = self._paths(key)
        if not os.path.exists(meta_path):
            return None
        with open(meta_path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def keys(self):
        out = []
        for sub in sorted(os.listdir(self.root)):
            subdir = os.path.join(self.root, sub)
            if not os.path.isdir(subdir):
                continue
            for name in sorted(os.listdir(subdir)):
                if name.endswith(".bin"):
                    out.append(name[:-4])
        return out
