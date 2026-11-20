"""Versioned, checksummed snapshots with whole-snapshot replacement."""

import json
from pathlib import Path

from .errors import ErrorCode, OpChainError
from .hashing import hash_json

SNAPSHOT_KIND = "opchain-snapshot"


def build_snapshot(kind, payload, previous_version=0):
    """Wrap payload with a monotonic version and a content checksum."""
    version = int(previous_version) + 1
    envelope = {
        "kind": kind,
        "version": version,
        "payload": payload,
    }
    envelope["checksum"] = hash_json(
        {"kind": kind, "version": version, "payload": payload}
    )
    return envelope


def write_snapshot(path, envelope):
    """Replace the old snapshot atomically; no incremental patching."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(envelope, handle, indent=2, sort_keys=True)
    tmp_path.replace(path)
    return path


def read_snapshot(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            envelope = json.load(handle)
    except (json.JSONDecodeError, OSError) as exc:
        raise OpChainError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"snapshot {path} is unreadable: {exc}",
            {"path": str(path)},
        )
    expected = hash_json(
        {
            "kind": envelope["kind"],
            "version": envelope["version"],
            "payload": envelope["payload"],
        }
    )
    if envelope.get("checksum") != expected:
        raise OpChainError(
            ErrorCode.SNAPSHOT_CORRUPT,
            f"checksum mismatch in snapshot {path}",
            {
                "path": str(path),
                "expected": expected,
                "actual": envelope.get("checksum"),
            },
        )
    return envelope


def replace_snapshot(path, kind, payload, previous_version=0):
    envelope = build_snapshot(kind, payload, previous_version)
    write_snapshot(path, envelope)
    return envelope
