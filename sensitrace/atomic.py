"""Atomic file replacement, integrity envelopes and cross-platform file locks.

All durable artefacts (checkpoints, diagnostic reports, flow graphs) are
written with the same crash-safe protocol:

1. serialise the payload to JSON and compute its SHA-256 checksum;
2. write a sealed *envelope* to a unique temp file in the target directory;
3. flush + fsync the temp file, verify the checksum by re-reading it;
4. atomically ``os.replace`` the temp file over the final path.

A forced termination can therefore leave at most a stale temp file; the final
path always points at a complete, checksum-verified envelope.  Concurrent
checkpoint writers are serialised with an advisory file lock that works on both
Windows (``msvcrt``) and POSIX (``fcntl``).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from typing import Any, Dict, Iterator

from .errors import E_LOCK, Diagnostic

ENVELOPE_VERSION = 1


class LockUnavailable(RuntimeError):
    """Raised when another process already holds the work-directory lock."""

    def diagnostic(self) -> Diagnostic:
        return Diagnostic(E_LOCK, "", 0, "checkpoint lock is held by another process")


class IntegrityError(RuntimeError):
    """Raised when a persisted envelope fails its checksum verification."""


def _checksum(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


def build_envelope(structure_version: int, rules_version: int,
                   payload: Dict[str, Any]) -> Dict[str, Any]:
    payload_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "envelope_version": ENVELOPE_VERSION,
        "structure_version": structure_version,
        "rules_version": rules_version,
        "checksum_sha256": _checksum(payload_bytes),
        "payload": payload,
    }


def verify_envelope(envelope: Dict[str, Any], expected_structure: int,
                    expected_rules: int) -> Dict[str, Any]:
    """Validate an envelope's shape, checksum and version compatibility."""

    if not isinstance(envelope, dict) or "payload" not in envelope:
        raise IntegrityError("checkpoint envelope is missing its payload")
    payload_bytes = json.dumps(
        envelope["payload"], sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    if envelope.get("checksum_sha256") != _checksum(payload_bytes):
        raise IntegrityError("checkpoint checksum mismatch (partial/corrupt write)")
    if envelope.get("structure_version") != expected_structure:
        raise IntegrityError("checkpoint structure version differs from current build")
    if envelope.get("rules_version") != expected_rules:
        raise IntegrityError("derivation rules version differs from current build")
    return envelope["payload"]


def atomic_write_json(path: str, envelope: Dict[str, Any],
                      crash_after_tmp: bool = False) -> None:
    """Crash-safe write of ``envelope`` to ``path``."""

    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(envelope, handle, sort_keys=True, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if crash_after_tmp:
            # Test hook: simulate a forced kill after the temp write but before
            # the atomic replace, leaving no final file and only a temp file.
            raise SystemExit("simulated forced termination before replace")
        # Re-read and verify the temp file before promoting it.
        with open(tmp_path, "r", encoding="utf-8") as handle:
            reread = json.load(handle)
        if reread != envelope:
            raise IntegrityError("temp file content verification failed")
        os.replace(tmp_path, path)
        _fsync_directory(directory)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def read_envelope(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _fsync_directory(directory: str) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return  # Windows: directory fsync is not available; replace is atomic.
    descriptor = os.open(directory, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def file_lock(lock_path: str) -> Iterator[None]:
    """Acquire an exclusive lock or raise :class:`LockUnavailable`.

    Non-blocking on purpose: a lock conflict is reported as ``E_LOCK`` rather
    than silently blocking the second writer.
    """

    os.makedirs(os.path.dirname(os.path.abspath(lock_path)), exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows hosts
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:  # pragma: no cover
                handle.close()
                raise LockUnavailable() from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                handle.close()
                raise LockUnavailable() from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()
