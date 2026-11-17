"""Atomic npz snapshots, version identification and tamper detection."""

from __future__ import annotations

import json
import os
import tempfile

import numpy as np

from .errors import SnapshotStaleError
from .util import array_fingerprint, digest_bytes


SNAPSHOT_FORMAT_VERSION = 1


def _payload_checksum(metadata: dict, arrays: dict) -> str:
    hasher_parts = [json.dumps(metadata, sort_keys=True, separators=(",", ":"))]
    for name in sorted(arrays):
        hasher_parts.append(name)
        hasher_parts.append(array_fingerprint(arrays[name]))
    return digest_bytes(hasher_parts)


def write_snapshot(path: str, payload: dict) -> None:
    """Write a temp file in the same directory, then atomically replace.

    A crash before ``os.replace`` leaves only the previous complete snapshot
    (or no snapshot), so a partial temp file can never become the recovery
    point.
    """
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    arrays = dict(payload["arrays"])
    metadata = dict(payload["metadata"])
    metadata["format_version"] = SNAPSHOT_FORMAT_VERSION
    metadata["checksum"] = _payload_checksum(
        {key: value for key, value in metadata.items() if key != "checksum"},
        arrays,
    )
    fd, tmp_path = tempfile.mkstemp(
        prefix=".snapshot-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez(
                handle,
                metadata=np.frombuffer(
                    json.dumps(metadata, sort_keys=True).encode("utf-8"),
                    dtype=np.uint8,
                ),
                **arrays,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def load_snapshot(path: str) -> dict:
    if not os.path.exists(path):
        raise SnapshotStaleError(f"snapshot not found: {path}")
    with np.load(path, allow_pickle=False) as loaded:
        metadata = json.loads(loaded["metadata"].tobytes().decode("utf-8"))
        arrays = {
            name: loaded[name]
            for name in loaded.files
            if name != "metadata"
        }
    stored_checksum = metadata.pop("checksum", None)
    actual_checksum = _payload_checksum(metadata, arrays)
    if stored_checksum != actual_checksum:
        raise SnapshotStaleError(
            "snapshot checksum mismatch: payload tampered or partially written"
        )
    if metadata.get("format_version") != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotStaleError(
            f"unsupported snapshot format version {metadata.get('format_version')}"
        )
    return {"metadata": metadata, "arrays": arrays}


def verify_snapshot_freshness(
    snapshot: dict,
    expected_config_fingerprint: str,
    expected_input_fingerprint: str,
) -> None:
    """Reject resume when config or inputs no longer match the snapshot."""
    metadata = snapshot["metadata"]
    if metadata.get("config_fingerprint") != expected_config_fingerprint:
        raise SnapshotStaleError(
            "config fingerprint mismatch: snapshot is stale"
        )
    if metadata.get("input_fingerprint") != expected_input_fingerprint:
        raise SnapshotStaleError(
            "input fingerprint mismatch: snapshot is stale"
        )


def tamper_snapshot_version(path: str, new_version: int) -> None:
    """Test helper: rewrite only the embedded version bytes.

    The checksum is intentionally left untouched, so the next load detects
    the tampering with ``E_SNAPSHOT_STALE`` instead of silently accepting a
    rewritten snapshot.
    """
    with np.load(path, allow_pickle=False) as loaded:
        metadata = json.loads(loaded["metadata"].tobytes().decode("utf-8"))
        arrays = {
            name: loaded[name]
            for name in loaded.files
            if name != "metadata"
        }
    metadata["global_version"] = int(new_version)
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(prefix=".snapshot-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            np.savez(
                handle,
                metadata=np.frombuffer(
                    json.dumps(metadata, sort_keys=True).encode("utf-8"),
                    dtype=np.uint8,
                ),
                **arrays,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise
