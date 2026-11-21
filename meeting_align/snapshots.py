"""Versioned snapshots for alignment config, seam sets and PCM digests.

Each snapshot carries a monotonically increasing version number and a
content checksum.  Any version/checksum change replaces the whole snapshot
atomically (never patched incrementally).  A version that does not advance
is rejected with E_SNAPSHOT_ROLLBACK; a checksum mismatch on load is
rejected with E_SNAPSHOT_CORRUPT.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import SnapshotCorruptError, SnapshotRollbackError
from .util import canonical_json, checksum_obj


class SnapshotStore:
    def __init__(self, directory):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.snapshot.json"

    def load(self, name: str):
        path = self._path(name)
        if not path.exists():
            return None
        try:
            snap = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SnapshotCorruptError(
                f"snapshot {name} is not valid JSON", snapshot=name
            ) from exc
        if snap.get("checksum") != checksum_obj(snap.get("payload")):
            raise SnapshotCorruptError(
                f"snapshot {name} checksum mismatch", snapshot=name
            )
        return snap

    def put(self, name: str, payload, version: int):
        """Whole-snapshot replacement with version monotonicity enforced."""
        current = self.load(name)
        if current is not None and version <= current["version"]:
            raise SnapshotRollbackError(
                f"snapshot {name} version {version} does not advance "
                f"past {current['version']}",
                snapshot=name,
                attempted_version=version,
                current_version=current["version"],
            )
        snap = {
            "name": name,
            "version": version,
            "checksum": checksum_obj(payload),
            "payload": payload,
        }
        tmp = self._path(name).with_suffix(".tmp")
        tmp.write_text(canonical_json(snap))
        os.replace(tmp, self._path(name))  # atomic whole-file replacement
        return snap

    def upsert(self, name: str, payload):
        """Replace the snapshot if content changed; bump the version."""
        current = self.load(name)
        checksum = checksum_obj(payload)
        if current is not None and current["checksum"] == checksum:
            return current, False
        version = 1 if current is None else current["version"] + 1
        return self.put(name, payload, version), True

    def version(self, name: str):
        snap = self.load(name)
        return None if snap is None else snap["version"]
