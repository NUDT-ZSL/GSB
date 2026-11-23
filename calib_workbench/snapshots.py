"""Versioned snapshot store with whole-snapshot replacement (requirement 6).

Each snapshot kind (``plan``, ``calibrators``, ``metrics``) keeps
immutable versioned files ``v<N>.json`` plus a ``current.json`` pointer
holding ``{"version", "checksum", "file"}``.  Rules:
  * versions are monotone increasing; committing an explicit version
    <= current raises E_SNAPSHOT_ROLLBACK and changes nothing;
  * any checksum change replaces the whole snapshot (never patched);
  * identical checksum -> no-op commit (current version reported);
  * plan commits are serialized by a non-blocking lock file; a
    concurrent commit immediately fails with E_STAGE_BUSY.
"""

from __future__ import annotations

import json
import os

from .errors import E_STAGE_BUSY, E_SNAPSHOT_ROLLBACK, WorkbenchError
from .util import content_hash

STAGE = "snapshot"


class _BusyLock:
    """Non-blocking mutual exclusion via O_CREAT|O_EXCL lock file."""

    def __init__(self, path):
        self.path = path
        self.fd = None

    def __enter__(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise WorkbenchError(
                E_STAGE_BUSY, STAGE,
                "another plan commit is in progress; later committer fails fast",
                details={"lock": self.path},
            )
        os.write(self.fd, str(os.getpid()).encode("ascii"))
        return self

    def __exit__(self, *exc):
        if self.fd is not None:
            os.close(self.fd)
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        return False


class SnapshotStore:
    def __init__(self, root):
        self.root = root
        os.makedirs(root, exist_ok=True)

    def _dir(self, kind):
        path = os.path.join(self.root, kind)
        os.makedirs(path, exist_ok=True)
        return path

    def current(self, kind):
        ptr = os.path.join(self._dir(kind), "current.json")
        if not os.path.exists(ptr):
            return None
        with open(ptr, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def read_version(self, kind, version):
        path = os.path.join(self._dir(kind), f"v{version}.json")
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)

    def commit(self, kind, body, version=None):
        """Commit ``body`` as the next version; returns the pointer dict.

        ``version`` may be supplied to assert the intended version;
        supplying a version <= current is a rollback attempt and raises
        E_SNAPSHOT_ROLLBACK.
        """
        if kind == "plan":
            with _BusyLock(os.path.join(self.root, "plan.commit.lock")):
                return self._commit_locked(kind, body, version)
        return self._commit_locked(kind, body, version)

    def _commit_locked(self, kind, body, version):
        cur = self.current(kind)
        cur_version = cur["version"] if cur else 0
        if version is not None and version <= cur_version:
            raise WorkbenchError(
                E_SNAPSHOT_ROLLBACK, STAGE,
                f"refusing to commit {kind} v{version} over current v{cur_version}",
                details={"kind": kind, "attempted": version, "current": cur_version},
            )
        checksum = content_hash(body)
        if cur and cur["checksum"] == checksum:
            return {**cur, "changed": False}
        new_version = cur_version + 1
        if version is not None and version > new_version:
            new_version = version
        record = {"version": new_version, "checksum": checksum, "body": body}
        kind_dir = self._dir(kind)
        path = os.path.join(kind_dir, f"v{new_version}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(record, fh, sort_keys=True, separators=(",", ":"), allow_nan=False)
        ptr = {"version": new_version, "checksum": checksum, "file": f"v{new_version}.json"}
        tmp = os.path.join(kind_dir, f"current.json.tmp.{os.getpid()}")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(ptr, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, os.path.join(kind_dir, "current.json"))
        return {**ptr, "changed": True}
