"""Content-addressed cache.

Every node artifact is stored under its content hash; a key file maps the
cache key to the content hash plus all key components, so the key can be
recomputed and cross-checked against the artifact hash (self-check).

Cache key = sha256 over canonical JSON of:
  node name, node params, input digests, upstream artifact hashes,
  label-set version (for nodes that depend on the label set).
"""
import json
import os
import pickle

from .hashing import canonical, sha256_hex


def make_key(node, params, inputs, upstream, labelset_version):
    components = {
        "node": node,
        "params": params,
        "inputs": inputs,
        "upstream": upstream,
        "labelset_version": labelset_version,
    }
    return sha256_hex(canonical(components)), components


class Cache:
    def __init__(self, root):
        self.root = root
        self.artifacts_dir = os.path.join(root, "artifacts")
        self.keys_dir = os.path.join(root, "keys")
        os.makedirs(self.artifacts_dir, exist_ok=True)
        os.makedirs(self.keys_dir, exist_ok=True)

    def _artifact_path(self, content_hash):
        return os.path.join(self.artifacts_dir, content_hash[:2], content_hash + ".bin")

    def _key_path(self, key):
        return os.path.join(self.keys_dir, key + ".json")

    def put(self, key, components, obj):
        data = pickle.dumps(obj, protocol=4)
        content_hash = sha256_hex(data)
        path = self._artifact_path(content_hash)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        meta = {
            "key": key,
            "components": components,
            "content_hash": content_hash,
            "artifact": os.path.relpath(path, self.root),
            "size": len(data),
        }
        tmp = self._key_path(key) + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(canonical(meta))
        os.replace(tmp, self._key_path(key))
        return content_hash

    def get(self, key):
        kp = self._key_path(key)
        if not os.path.exists(kp):
            return None
        with open(kp) as fh:
            meta = json.load(fh)
        path = os.path.join(self.root, meta["artifact"])
        if not os.path.exists(path):
            return None
        with open(path, "rb") as fh:
            data = fh.read()
        if sha256_hex(data) != meta["content_hash"]:
            return None
        return pickle.loads(data), meta["content_hash"]

    def explain(self, key):
        """Provenance query: hit/miss, source artifact, key components."""
        kp = self._key_path(key)
        if not os.path.exists(kp):
            return {"key": key, "hit": False}
        with open(kp) as fh:
            meta = json.load(fh)
        path = os.path.join(self.root, meta["artifact"])
        ok = os.path.exists(path)
        if ok:
            with open(path, "rb") as fh:
                ok = sha256_hex(fh.read()) == meta["content_hash"]
        return {
            "key": key,
            "hit": ok,
            "content_hash": meta["content_hash"],
            "artifact": meta["artifact"],
            "size": meta["size"],
            "components": meta["components"],
        }

    def iter_keys(self):
        for name in sorted(os.listdir(self.keys_dir)):
            if name.endswith(".json"):
                yield name[:-5]

    def verify(self, key):
        """Cross-check: recompute key from components, verify artifact hash."""
        kp = self._key_path(key)
        if not os.path.exists(kp):
            return False, "key file missing"
        with open(kp) as fh:
            meta = json.load(fh)
        recomputed, _ = make_key(**meta["components"])
        if recomputed != key:
            return False, "key does not match components"
        path = os.path.join(self.root, meta["artifact"])
        if not os.path.exists(path):
            return False, "artifact missing"
        with open(path, "rb") as fh:
            if sha256_hex(fh.read()) != meta["content_hash"]:
                return False, "artifact hash mismatch"
        return True, "ok"
