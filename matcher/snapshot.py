"""Stage 7: versioned snapshots, atomic batch commits, concurrency control."""
import json
import os

from .errors import (
    BusinessError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .models import canonical_json, content_hash, write_atomic


def file_checksum(path):
    import hashlib

    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SnapshotStore:
    """Tracks (version, checksum) per snapshotted artifact.

    Any version/checksum change replaces the whole snapshot; rollback
    attempts and checksum corruption are hard errors.
    """

    def __init__(self, path):
        self.path = path
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self._entries = json.load(fh)
        else:
            self._entries = {}

    def _save(self):
        write_atomic(self.path, canonical_json(self._entries))

    def get(self, name):
        return self._entries.get(name)

    def verify(self, name):
        """Raise E_SNAPSHOT_CORRUPT if the recorded artifact was tampered."""
        entry = self._entries.get(name)
        if entry is None:
            return None
        path = entry["path"]
        if not os.path.exists(path) or file_checksum(path) != entry["checksum"]:
            raise BusinessError(
                E_SNAPSHOT_CORRUPT,
                "snapshot %r failed checksum verification" % name,
                field="checksum",
                context={"artifact": name, "path": path},
            )
        return entry

    def update(self, name, version, path):
        """Record a new whole snapshot; reject version rollback."""
        entry = self._entries.get(name)
        version = int(version)
        if entry is not None and version < int(entry["version"]):
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot %r version %d regresses below committed %d"
                % (name, version, entry["version"]),
                field="version",
                context={"artifact": name, "version": version,
                         "committed": entry["version"]},
            )
        checksum = file_checksum(path)
        same = (
            entry is not None
            and version == int(entry["version"])
            and checksum == entry["checksum"]
        )
        if same:
            return entry
        self._entries[name] = {
            "version": version,
            "checksum": checksum,
            "path": path,
        }
        self._save()
        return self._entries[name]


class StageLock:
    """Single-committer lock: only one ledger commit may be in flight."""

    def __init__(self, lock_path):
        self.lock_path = lock_path
        self._fd = None

    def acquire(self):
        try:
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BusinessError(
                E_STAGE_BUSY,
                "another ledger commit is in flight",
                field="lock",
                context={"lock": self.lock_path},
            ) from exc
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.remove(self.lock_path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


class BatchCommitter:
    """Atomic per-batch commits with a monotonic HEAD marker for recovery."""

    def __init__(self, commit_dir):
        self.commit_dir = commit_dir
        self.batches_dir = os.path.join(commit_dir, "batches")
        os.makedirs(self.batches_dir, exist_ok=True)
        self.head_path = os.path.join(commit_dir, "HEAD")
        self.key_path = os.path.join(commit_dir, "batch_key")

    def job_key(self):
        if not os.path.exists(self.key_path):
            return None
        with open(self.key_path, "r", encoding="utf-8") as fh:
            return fh.read().strip()

    def set_job_key(self, key):
        write_atomic(self.key_path, key)

    def committed_batch(self):
        if not os.path.exists(self.head_path):
            return -1
        with open(self.head_path, "r", encoding="utf-8") as fh:
            return int(json.load(fh)["batch_id"])

    def reset(self):
        """Drop all committed batches (input fingerprint changed)."""
        import shutil

        if os.path.exists(self.batches_dir):
            shutil.rmtree(self.batches_dir)
        os.makedirs(self.batches_dir, exist_ok=True)
        if os.path.exists(self.head_path):
            os.remove(self.head_path)
        if os.path.exists(self.key_path):
            os.remove(self.key_path)

    def commit(self, batch_id, payload):
        if batch_id <= self.committed_batch():
            raise BusinessError(
                E_SNAPSHOT_ROLLBACK,
                "batch %d already committed" % batch_id,
                field="batch_id",
            )
        write_atomic(
            os.path.join(self.batches_dir, "batch_%05d.json" % batch_id),
            canonical_json(payload),
        )
        write_atomic(self.head_path, canonical_json({"batch_id": batch_id}))

    def load_batch(self, batch_id):
        with open(
            os.path.join(self.batches_dir, "batch_%05d.json" % batch_id),
            "r",
            encoding="utf-8",
        ) as fh:
            return json.load(fh)
