"""Atomic persistence, file locking and checkpoint recovery.

Writes go to a temporary file in the same directory, are flushed, fsynced
and verified before an atomic rename replaces the target. A checkpoint is
an envelope {structure_version, payload_sha256, payload}; a half-written
or corrupted file is never accepted as valid state.
"""

from __future__ import annotations

import hashlib
import json
import os

from . import STRUCTURE_VERSION, errors
from .models import canonical_json


class LockError(errors.CertTrustError):
    def __init__(self, path):
        super().__init__(errors.E_LOCK,
                         f"work directory is locked by another process: {path}")


class FileLock:
    """Cross-platform advisory lock via an exclusively-created lock file."""

    def __init__(self, path):
        self.path = path
        self._fd = None

    def acquire(self):
        try:
            self._fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise LockError(self.path)
        os.write(self._fd, str(os.getpid()).encode("ascii"))
        return self

    def release(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *exc):
        self.release()
        return False


def _digest(payload) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def wrap_payload(payload) -> dict:
    return {
        "structure_version": STRUCTURE_VERSION,
        "payload_sha256": _digest(payload),
        "payload": payload,
    }


def atomic_write_json(path, obj):
    """Write obj as JSON atomically; verify integrity before replacing."""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    tmp_path = os.path.join(directory, os.path.basename(path) + ".tmp")
    data = json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    with open(tmp_path, "r", encoding="utf-8") as fh:
        reread = json.load(fh)
    if canonical_json(reread) != canonical_json(obj):
        os.unlink(tmp_path)
        raise errors.CertTrustError(
            errors.E_CHECKPOINT_CORRUPT,
            f"integrity check failed before replacing {path}")
    os.replace(tmp_path, path)


def write_checkpoint(path, payload):
    atomic_write_json(path, wrap_payload(payload))


def read_checkpoint(path):
    """Load and verify a checkpoint envelope.

    Returns the payload dict, or None when no usable checkpoint exists
    (absent, half-written, digest mismatch, unsupported structure version).
    Temporary files are never considered valid state.
    """
    try:
        with open(path, "r", encoding="utf-8") as fh:
            envelope = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("structure_version") != STRUCTURE_VERSION:
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    if envelope.get("payload_sha256") != _digest(payload):
        return None
    return payload
