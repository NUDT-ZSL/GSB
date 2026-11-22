"""Versioned snapshot stores with monotonic versions and checksums.

Label sets, projection params and metric statistics each live in their own
snapshot file: {version, checksum, payload}. Installs replace the whole
snapshot atomically (never incremental patching). Rollbacks are rejected
with E_SNAPSHOT_ROLLBACK; checksum mismatches with E_CACHE_CORRUPT.
"""
import json
import os

from .errors import BusinessError, E_CACHE_CORRUPT, E_SNAPSHOT_ROLLBACK
from .hashing import canonical, digest_obj


class SnapshotStore:
    def __init__(self, path, node):
        self.path = path
        self.node = node  # owner node name, reported in E_CACHE_CORRUPT

    @staticmethod
    def checksum(payload):
        return digest_obj(payload)

    def _read_raw(self):
        if not os.path.exists(self.path):
            return None
        with open(self.path) as fh:
            return json.load(fh)

    def load(self):
        raw = self._read_raw()
        if raw is None:
            return None
        if raw.get("checksum") != self.checksum(raw.get("payload")):
            raise BusinessError(
                E_CACHE_CORRUPT, "snapshot checksum mismatch", node=self.node)
        return raw

    def current_version(self):
        raw = self.load()
        return raw["version"] if raw else 0

    def install(self, version, payload):
        """Wholly replace the snapshot; reject rollback or corrupted state."""
        current = self.load()  # raises E_CACHE_CORRUPT if unreadable
        if current is not None and version <= current["version"]:
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot version must be strictly increasing",
                node=self.node, current=current["version"], attempted=version)
        doc = {
            "version": version,
            "checksum": self.checksum(payload),
            "payload": payload,
        }
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(canonical(doc))
        os.replace(tmp, self.path)
        return doc
