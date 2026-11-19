"""Versioned algorithm-path snapshots with content checksums.

Each snapshot entry carries a monotonically increasing version, a SHA-256
checksum of its canonical JSON content and the serialized path / configuration
definition. Loading verifies the checksum; a mismatch or any structural damage
raises ``E_SNAPSHOT_CORRUPT``. When a newer snapshot is saved it wholly
replaces the previous snapshot directory contents.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil

from .errors import BusinessError, ErrorCode

SNAPSHOT_FILE = "path_snapshot.json"


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def checksum(payload: dict) -> str:
    return hashlib.sha256(_canonical_bytes(payload)).hexdigest()


def build_snapshot(
    version: int,
    config_dict: dict,
    path_definitions: dict,
    decision_dict: dict,
) -> dict:
    content = {
        "config": config_dict,
        "path_definitions": path_definitions,
        "decision": decision_dict,
    }
    return {
        "version": int(version),
        "checksum": checksum(content),
        "content": content,
    }


def save_snapshot(snapshot_dir: str, snapshot: dict) -> str:
    """Wholly replace the snapshot directory and write the new version."""
    if not isinstance(snapshot.get("version"), int) or snapshot["version"] <= 0:
        raise BusinessError(
            ErrorCode.E_SNAPSHOT_CORRUPT,
            "snapshot version must be a positive monotonic integer",
            field="version",
        )
    existing_version = 0
    existing_path = os.path.join(snapshot_dir, SNAPSHOT_FILE)
    if os.path.isfile(existing_path):
        try:
            old = load_snapshot(snapshot_dir)
            existing_version = int(old["version"])
        except BusinessError:
            existing_version = 0
    if snapshot["version"] < existing_version:
        raise BusinessError(
            ErrorCode.E_SNAPSHOT_CORRUPT,
            f"new version {snapshot['version']} is older than existing "
            f"{existing_version}; monotonic ordering violated",
            field="version",
        )
    if os.path.isdir(snapshot_dir):
        shutil.rmtree(snapshot_dir)
    os.makedirs(snapshot_dir, exist_ok=True)
    path = os.path.join(snapshot_dir, SNAPSHOT_FILE)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(snapshot, fh, sort_keys=True, ensure_ascii=False, indent=2)
        fh.write("\n")
    return path


def load_snapshot(snapshot_dir: str) -> dict:
    path = os.path.join(snapshot_dir, SNAPSHOT_FILE)
    if not os.path.isfile(path):
        raise BusinessError(
            ErrorCode.E_SNAPSHOT_CORRUPT,
            f"snapshot file missing: {path}",
            field="snapshot",
        )
    try:
        with open(path, "r", encoding="utf-8") as fh:
            snapshot = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        raise BusinessError(
            ErrorCode.E_SNAPSHOT_CORRUPT,
            f"snapshot cannot be parsed: {exc}",
            field="snapshot",
        ) from exc
    for key in ("version", "checksum", "content"):
        if key not in snapshot:
            raise BusinessError(
                ErrorCode.E_SNAPSHOT_CORRUPT,
                f"snapshot missing required key {key!r}",
                field=key,
            )
    if not isinstance(snapshot["version"], int) or snapshot["version"] <= 0:
        raise BusinessError(
            ErrorCode.E_SNAPSHOT_CORRUPT,
            "illegal snapshot version",
            field="version",
        )
    actual = checksum(snapshot["content"])
    if actual != snapshot["checksum"]:
        raise BusinessError(
            ErrorCode.E_SNAPSHOT_CORRUPT,
            "content checksum mismatch; snapshot is damaged or tampered",
            details={
                "expected": snapshot["checksum"],
                "actual": actual,
            },
        )
    return snapshot
