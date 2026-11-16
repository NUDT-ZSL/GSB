"""Atomic snapshot persistence and stale-snapshot recovery decisions."""

import json
import os
import tempfile

from .errors import E_SNAPSHOT_STALE

SNAPSHOT_NAME = "snapshot.json"
SNAPSHOT_FORMAT_VERSION = 1


class SnapshotStore:
    def __init__(self, directory):
        self.directory = directory
        self.path = os.path.join(directory, SNAPSHOT_NAME)
        self.version = 0
        os.makedirs(directory, exist_ok=True)

    def write(self, payload):
        self.version += 1
        document = {
            "snapshot_format_version": SNAPSHOT_FORMAT_VERSION,
            "payload": payload,
        }
        fd, tmp_path = tempfile.mkstemp(
            prefix=".snapshot.", suffix=".tmp", dir=self.directory
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, sort_keys=True, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise
        return self.version

    def exists(self):
        return os.path.exists(self.path)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)["payload"]

    def restore_or_rebuild(self, engine):
        """Return recovery metadata; rebuild from empty on stale snapshots."""
        if not self.exists():
            engine.mark_stale("snapshot_missing")
            return {"code": E_SNAPSHOT_STALE, "reason": "snapshot_missing"}
        try:
            payload = self.read()
        except (json.JSONDecodeError, KeyError, OSError) as exc:
            engine.mark_stale(f"corrupt_snapshot:{type(exc).__name__}")
            return {
                "code": E_SNAPSHOT_STALE,
                "reason": f"corrupt_snapshot:{type(exc).__name__}",
            }
        ok = engine.restore_payload(payload)
        if not ok:
            engine.mark_stale("config_or_vocab_hash_mismatch")
            return {
                "code": E_SNAPSHOT_STALE,
                "reason": "config_or_vocab_hash_mismatch",
            }
        self.version = engine.snapshot_version
        return {"code": None, "reason": None, "version": self.version}

    @staticmethod
    def clean(directory):
        if not os.path.isdir(directory):
            return
        path = os.path.join(directory, SNAPSHOT_NAME)
        if os.path.exists(path):
            os.unlink(path)
        for name in os.listdir(directory):
            if name.startswith(".snapshot.") and name.endswith(".tmp"):
                os.unlink(os.path.join(directory, name))
