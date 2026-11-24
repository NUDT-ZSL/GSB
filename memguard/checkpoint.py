"""Checkpoint snapshots, atomic replacement and cross-platform file lock."""

from __future__ import annotations

import json
import os

from . import CHECKPOINT_STRUCT_VERSION, SNAPSHOT_VERSION, codes

if os.name == "nt":  # pragma: no cover - exercised on Windows
    import msvcrt
else:
    import fcntl


class LockConflict(Exception):
    code = codes.E_LOCK


class SnapshotVersionMismatch(Exception):
    code = codes.E_VERSION


class FileLock:
    """Non-blocking advisory lock on <workdir>/guard.lock."""

    def __init__(self, workdir):
        self.path = os.path.join(workdir, "guard.lock")
        self._fh = None

    def acquire(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            if os.name == "nt":  # pragma: no cover
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            raise LockConflict(f"lock busy: {self.path}")
        self._fh = fh
        return self

    def release(self):
        if self._fh is not None:
            try:
                if os.name == "nt":  # pragma: no cover
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def atomic_write_json(path, payload):
    """Write JSON via temp file, validate by re-parsing, then os.replace."""
    tmp = f"{path}.tmp.{os.getpid()}"
    text = json.dumps(payload, indent=2, sort_keys=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    with open(tmp, "r", encoding="utf-8") as fh:  # validate before replace
        json.load(fh)
    os.replace(tmp, path)


def snapshot_path(workdir):
    return os.path.join(workdir, "guard.snapshot.json")


def write_snapshot(workdir, batch_seq, digest, ledger_summary):
    payload = {
        "snapshot_version": SNAPSHOT_VERSION,
        "checkpoint_struct_version": CHECKPOINT_STRUCT_VERSION,
        "batch_seq": batch_seq,
        "ledger_digest": digest,
        "ledger_summary": ledger_summary,
    }
    atomic_write_json(snapshot_path(workdir), payload)
    return payload


def read_snapshot(workdir):
    """Load the snapshot; raises SnapshotVersionMismatch on version drift."""
    path = snapshot_path(workdir)
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if payload.get("snapshot_version") != SNAPSHOT_VERSION:
        raise SnapshotVersionMismatch(
            f"snapshot_version {payload.get('snapshot_version')} != {SNAPSHOT_VERSION}")
    if payload.get("checkpoint_struct_version") != CHECKPOINT_STRUCT_VERSION:
        raise SnapshotVersionMismatch("checkpoint struct version changed")
    return payload
