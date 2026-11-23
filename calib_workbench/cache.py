"""Content-addressable artifact cache (requirement 7).

Cache keys are SHA-256 digests of the canonical JSON of
``{"node", "config_version", "inputs"}`` so any change to an upstream
artifact or relevant config subset produces a different key, while
unchanged upstreams are reused byte-for-byte.
"""

from __future__ import annotations

import json
import os

from .util import content_hash


def cache_key(node: str, inputs) -> str:
    """Content-address of a node: keyed on the *content* of its inputs.

    The config version is intentionally NOT part of the key: a version
    bump that leaves the relevant config subset untouched must reuse the
    artifact, while any real content change yields a new key (precise
    invalidation).  The version is recorded in lineage/snapshots instead.
    """
    return content_hash({"node": node, "inputs": inputs})


class FileCache:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _path(self, key):
        return os.path.join(self.root, key[:2], f"{key}.json")

    def has(self, key) -> bool:
        return os.path.exists(self._path(key))

    def get(self, key):
        with open(self._path(key), "r", encoding="utf-8") as fh:
            return json.load(fh)

    def put(self, key, value) -> None:
        path = self._path(key)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, sort_keys=True, separators=(",", ":"), allow_nan=False)
        os.replace(tmp, path)
