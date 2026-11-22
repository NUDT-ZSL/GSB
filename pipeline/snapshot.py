"""Versioned, checksummed, atomically-committed snapshot storage.

Layout under the store directory:
    manifest.json            committed state (written via tmp+os.replace)
    stages/<stage>.snap.json committed per-stage snapshot files
    wip/                     uncommitted scratch (never trusted on load)

Snapshot file format (JSON, keys sorted, LF newlines):
    {"header": {"stage": ..., "version": ..., "committed_docs": [...],
                "checksum": "sha256-of-canonical-payload"},
     "payload": {...}}
"""

import hashlib
import json
import os
import shutil
import threading

from .errors import BuildBusyError, SnapshotCorruptError, SnapshotRollbackError

STAGES = ("ingest", "compress", "stats")


def canonical_bytes(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")


def checksum_of(payload):
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()


def _atomic_write(path, data_bytes):
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data_bytes)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class SnapshotStore:
    """Persistent store of committed stage snapshots.

    Commit is guarded by a non-blocking build-commit lock: a second
    concurrent committer gets E_BUILD_BUSY instead of waiting.
    """

    def __init__(self, root):
        self.root = root
        self.stages_dir = os.path.join(root, "stages")
        self.wip_dir = os.path.join(root, "wip")
        self.manifest_path = os.path.join(root, "manifest.json")
        self._commit_lock = threading.Lock()
        for d in (self.stages_dir, self.wip_dir):
            os.makedirs(d, exist_ok=True)
        self._manifest = self._load_manifest()

    def _load_manifest(self):
        if not os.path.exists(self.manifest_path):
            return {"committed": {}}
        with open(self.manifest_path, "rb") as fh:
            return json.loads(fh.read().decode("utf-8"))

    def _write_manifest(self):
        _atomic_write(self.manifest_path, canonical_bytes(self._manifest))

    def commit(self, stage, version, committed_docs, payload):
        """Atomically commit a stage snapshot. Returns the snapshot header.

        Raises E_BUILD_BUSY if another commit is in flight, and
        E_SNAPSHOT_ROLLBACK if the store already holds a newer/equal
        version for this stage.
        """
        if stage not in STAGES:
            raise SnapshotCorruptError(stage, version, "unknown stage name")
        if not self._commit_lock.acquire(blocking=False):
            raise BuildBusyError("another build commit is in progress",
                                 stage=stage)
        try:
            committed = self._manifest["committed"]
            if stage in committed:
                cur = committed[stage]
                if version <= cur["version"]:
                    raise SnapshotRollbackError(
                        "refusing to overwrite newer/equal committed stage",
                        stage=stage, existing_version=cur["version"],
                        attempted_version=version)
            header = {
                "stage": stage,
                "version": version,
                "committed_docs": sorted(committed_docs),
                "checksum": checksum_of(payload),
            }
            record = {"header": header, "payload": payload}
            path = os.path.join(self.stages_dir, f"{stage}.snap.json")
            _atomic_write(path, canonical_bytes(record))
            committed[stage] = {
                "version": version,
                "checksum": header["checksum"],
                "payload_ref": payload,
            }
            self._write_manifest()
            return header
        finally:
            self._commit_lock.release()

    def load(self, stage):
        """Load a committed snapshot; verifies version monotonicity and
        checksum. Corruption raises E_SNAPSHOT_CORRUPT; nothing partially
        parsed is returned."""
        path = os.path.join(self.stages_dir, f"{stage}.snap.json")
        if not os.path.exists(path):
            return None
        record = None
        try:
            with open(path, "rb") as fh:
                record = json.loads(fh.read().decode("utf-8"))
            header = record["header"]
            payload = record["payload"]
            assert header["stage"] == stage
            assert isinstance(header["version"], int)
        except SnapshotCorruptError:
            raise
        except Exception as exc:
            ver = -1
            if isinstance(record, dict):
                try:
                    ver = record["header"]["version"]
                except Exception:
                    pass
            raise SnapshotCorruptError(stage, ver, f"unparseable: {exc}")
        if checksum_of(payload) != header["checksum"]:
            raise SnapshotCorruptError(stage, header["version"],
                                       "payload checksum mismatch")
        committed = self._manifest["committed"].get(stage)
        if committed and header["version"] < committed["version"]:
            raise SnapshotRollbackError(
                "snapshot file older than manifest", stage=stage,
                file_version=header["version"],
                manifest_version=committed["version"])
        return record

    def load_all_committed(self):
        """Return {stage: record} for every committed stage, in stage order."""
        out = {}
        for stage in STAGES:
            rec = self.load(stage)
            if rec is not None:
                out[stage] = rec
        return out

    def committed_version(self, stage):
        info = self._manifest["committed"].get(stage)
        return info["version"] if info else 0

    def wip_path(self, name):
        return os.path.join(self.wip_dir, name)

    def discard_wip(self):
        """Drop all uncommitted intermediate state."""
        if os.path.isdir(self.wip_dir):
            shutil.rmtree(self.wip_dir)
        os.makedirs(self.wip_dir, exist_ok=True)
