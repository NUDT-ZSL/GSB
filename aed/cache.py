"""Content-addressed cache and versioned snapshot store.

- Cache keys hash the full component dict (audio chunk hashes, template
  version, hop, min-duration/gap params, upstream node hashes).
- Every artifact is stored with its content hash; reads verify it.
- Snapshots (cache entries, template library, result summaries) carry a
  monotonic version and checksum and are replaced wholesale.
- Concurrent commits fail fast with E_STAGE_BUSY (non-blocking lock).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading

from .errors import AEDError, Code


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def make_key(components: dict) -> str:
    return sha256_bytes(canonical_json(components))


def audio_chunk_hashes(pcm_bytes: bytes, n_chunks: int = 8) -> list:
    """Content hashes of fixed audio chunks (part of every root key)."""
    n_chunks = max(1, min(n_chunks, len(pcm_bytes) or 1))
    size = -(-len(pcm_bytes) // n_chunks)
    hashes = [sha256_bytes(pcm_bytes[i:i + size])
              for i in range(0, len(pcm_bytes), size)]
    return hashes or [sha256_bytes(b"")]


class ContentCache:
    """Content-addressed artifact store under a local cache directory."""

    def __init__(self, cache_dir: str, max_entries: int = 1000):
        self.dir = cache_dir
        self.max_entries = max_entries
        os.makedirs(self.dir, exist_ok=True)
        if not os.access(self.dir, os.W_OK):
            raise AEDError(Code.E_RESOURCE_LIMIT,
                           "cache directory is not writable",
                           cache_dir=self.dir)

    def _meta_path(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.json")

    def _blob_path(self, key: str) -> str:
        return os.path.join(self.dir, f"{key}.bin")

    def get(self, key: str):
        """Return artifact bytes or None; verify content checksum."""
        meta_path, blob_path = self._meta_path(key), self._blob_path(key)
        if not (os.path.exists(meta_path) and os.path.exists(blob_path)):
            return None
        with open(meta_path, "r", encoding="utf-8") as fh:
            meta = json.load(fh)
        with open(blob_path, "rb") as fh:
            blob = fh.read()
        if sha256_bytes(blob) != meta["artifact_hash"]:
            raise AEDError(Code.E_CACHE_CORRUPT,
                           "artifact checksum mismatch",
                           key=key, node=meta.get("node"))
        return blob

    def put(self, node: str, components: dict, payload: bytes) -> str:
        key = make_key(components)
        artifact_hash = sha256_bytes(payload)
        existing = self._read_meta(key)
        version = (existing["version"] + 1) if existing else 1
        tmp = self._blob_path(key) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(payload)
        os.replace(tmp, self._blob_path(key))
        meta = {"node": node, "key": key, "components": components,
                "artifact_hash": artifact_hash, "version": version}
        tmp = self._meta_path(key) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, sort_keys=True)
        os.replace(tmp, self._meta_path(key))
        self._enforce_limit()
        return key

    def _read_meta(self, key: str):
        path = self._meta_path(key)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def _enforce_limit(self) -> None:
        metas = self._all_metas()
        if len(metas) <= self.max_entries:
            return
        metas.sort(key=lambda m: (m["version"], m["key"]))
        for meta in metas[:len(metas) - self.max_entries]:
            for path in (self._meta_path(meta["key"]),
                         self._blob_path(meta["key"])):
                if os.path.exists(path):
                    os.remove(path)

    def _all_metas(self) -> list:
        out = []
        for name in os.listdir(self.dir):
            if name.endswith(".json"):
                with open(os.path.join(self.dir, name),
                          "r", encoding="utf-8") as fh:
                    out.append(json.load(fh))
        return out

    def describe(self, node: str) -> list:
        """Query key composition and artifact hash by node name."""
        return [m for m in self._all_metas() if m["node"] == node]


class SnapshotStore:
    """Versioned snapshots with whole-replacement commits.

    Used for cache-entry indexes, the template library and detection
    result summaries. Rollbacks, corruption and concurrent commits are
    rejected with structured business codes.
    """

    _locks: dict = {}
    _locks_guard = threading.Lock()

    def __init__(self, snap_dir: str):
        self.dir = snap_dir
        os.makedirs(self.dir, exist_ok=True)

    def _path(self, name: str) -> str:
        return os.path.join(self.dir, f"{name}.snap.json")

    def _lock_for(self, name: str) -> threading.Lock:
        with SnapshotStore._locks_guard:
            return SnapshotStore._locks.setdefault(
                os.path.abspath(self._path(name)), threading.Lock())

    def read(self, name: str) -> dict:
        path = self._path(name)
        if not os.path.exists(path):
            return {"version": 0, "checksum": None, "payload": None}
        with open(path, "r", encoding="utf-8") as fh:
            snap = json.load(fh)
        payload_bytes = canonical_json(snap["payload"])
        if sha256_bytes(payload_bytes) != snap["checksum"]:
            raise AEDError(Code.E_CACHE_CORRUPT,
                           "snapshot checksum mismatch",
                           snapshot=name, version=snap["version"])
        return snap

    def commit(self, name: str, payload, expected_version=None) -> dict:
        """Whole-replacement commit; non-blocking conflict detection."""
        lock = self._lock_for(name)
        if not lock.acquire(blocking=False):
            raise AEDError(Code.E_STAGE_BUSY,
                           "concurrent commit in progress", snapshot=name)
        try:
            current = self.read(name)
            if expected_version is not None and \
                    expected_version < current["version"]:
                raise AEDError(Code.E_SNAPSHOT_ROLLBACK,
                               "refusing to replace a newer snapshot",
                               snapshot=name,
                               current_version=current["version"],
                               attempted_version=expected_version)
            snap = {"version": current["version"] + 1,
                    "checksum": sha256_bytes(canonical_json(payload)),
                    "payload": payload}
            tmp = self._path(name) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, sort_keys=True)
            os.replace(tmp, self._path(name))
            return snap
        finally:
            lock.release()
