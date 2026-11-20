"""Content-addressed artifact cache with versioned snapshots.

Layout under the cache root:
  artifacts/<key>.bin   deterministic artifact bytes (codec.dumps output)
  locks/<key>.lock      O_EXCL commit lock, one winner per cache key
  snapshot.json         {"version", "entries", "checksum"}

The snapshot carries a monotonically increasing version and a checksum over
{version, entries}. Whenever the on-disk version/checksum differs from the
in-memory view, the in-memory snapshot is replaced wholesale - never patched
incrementally. A corrupt or checksum-mismatching snapshot raises
E_CACHE_CORRUPT. Concurrent commits of the same key are serialized by the
lock file: the loser gets E_CACHE_BUSY immediately instead of blocking.
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path

from . import errors
from .codec import hash_obj, sha256_bytes


class ArtifactCache:
    def __init__(self, root, max_entries=4096):
        self.root = Path(root)
        self.artifacts_dir = self.root / "artifacts"
        self.locks_dir = self.root / "locks"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.max_entries = int(max_entries)
        self.degraded = False
        self._snapshot = None
        self._snapshot_sig = None

    @property
    def snapshot_path(self):
        return self.root / "snapshot.json"

    def _read_snapshot_file(self):
        if not self.snapshot_path.exists():
            return {"version": 0, "entries": {}}
        raw = self.snapshot_path.read_bytes()
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise errors.VadError(
                errors.E_CACHE_CORRUPT,
                "snapshot is not valid JSON",
                path=str(self.snapshot_path),
            ) from exc
        body = {"version": obj.get("version"), "entries": obj.get("entries")}
        if not isinstance(body["version"], int) or not isinstance(body["entries"], dict):
            raise errors.VadError(
                errors.E_CACHE_CORRUPT,
                "snapshot is missing version/entries",
                path=str(self.snapshot_path),
            )
        if hash_obj(body) != obj.get("checksum"):
            raise errors.VadError(
                errors.E_CACHE_CORRUPT,
                "snapshot checksum mismatch",
                path=str(self.snapshot_path),
            )
        return body

    def _reload_if_changed(self):
        body = self._read_snapshot_file()
        sig = (body["version"], hash_obj(body))
        if sig != self._snapshot_sig:
            # wholesale replacement of the in-memory snapshot, never a patch
            self._snapshot = body
            self._snapshot_sig = sig

    @property
    def version(self):
        self._reload_if_changed()
        return self._snapshot["version"]

    def entries(self):
        self._reload_if_changed()
        return dict(self._snapshot["entries"])

    def get(self, key):
        self._reload_if_changed()
        meta = self._snapshot["entries"].get(key)
        if meta is None:
            return None
        path = self.artifacts_dir / meta["file"]
        if not path.exists():
            raise errors.VadError(
                errors.E_CACHE_CORRUPT,
                "snapshot references a missing artifact",
                key=key,
                file=meta["file"],
            )
        data = path.read_bytes()
        if sha256_bytes(data) != meta["sha256"]:
            raise errors.VadError(
                errors.E_CACHE_CORRUPT,
                "artifact checksum mismatch",
                key=key,
                file=meta["file"],
            )
        return data

    def commit(self, key, data):
        """Commit artifact bytes under ``key``; exactly one concurrent writer wins."""
        lock = self.locks_dir / f"{key}.lock"
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise errors.VadError(
                errors.E_CACHE_BUSY,
                "cache key is being committed by another task",
                key=key,
            ) from exc
        try:
            self._reload_if_changed()
            if key in self._snapshot["entries"]:
                return
            if len(self._snapshot["entries"]) >= self.max_entries:
                # degrade: keep computing, just stop caching new entries
                self.degraded = True
                return
            fname = f"{key}.bin"
            tmp = self.artifacts_dir / f".{fname}.tmp"
            tmp.write_bytes(data)
            os.replace(tmp, self.artifacts_dir / fname)
            meta = {"file": fname, "size": len(data), "sha256": sha256_bytes(data)}
            self._merge_entry(key, meta)
        finally:
            os.close(fd)
            try:
                os.unlink(lock)
            except FileNotFoundError:
                pass

    def _merge_entry(self, key, meta):
        """Serialized read-modify-write of the snapshot (short critical section).

        The per-key O_EXCL lock keeps same-key commits non-blocking; this lock
        only serializes the snapshot merge so concurrent processes cannot lose
        each other's entries.
        """
        lock_path = self.root / "snapshot.lock"
        sfd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(sfd, fcntl.LOCK_EX)
            self._reload_if_changed()
            if key in self._snapshot["entries"]:
                return
            entries = dict(self._snapshot["entries"])
            entries[key] = meta
            body = {"version": self._snapshot["version"] + 1, "entries": entries}
            snap = dict(body)
            snap["checksum"] = hash_obj(body)
            stmp = self.root / f".snapshot.json.tmp.{os.getpid()}"
            stmp.write_bytes(json.dumps(snap, sort_keys=True).encode("utf-8"))
            os.replace(stmp, self.snapshot_path)
            self._snapshot = body
            self._snapshot_sig = (body["version"], hash_obj(body))
        finally:
            fcntl.flock(sfd, fcntl.LOCK_UN)
            os.close(sfd)
