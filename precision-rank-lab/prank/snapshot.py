"""Versioned, checksummed configuration and statistics snapshots.

A snapshot is a self-describing JSON envelope::

    {
      "snapshot_category": "config" | "statistics",
      "version": <monotonic increasing integer>,
      "checksum": "sha256:<hex of canonical payload>",
      "payload": { ... }
    }

Submission always replaces the whole file atomically; incremental patching is
never performed. Load failures raise ``E_SNAPSHOT_CORRUPT`` carrying the
snapshot category and the version that failed verification.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from .errors import BusinessError


def canonical_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def checksum_of(payload: dict[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(payload)).hexdigest()


def submit_snapshot(path: str | Path, category: str, payload: dict[str, Any],
                    version: int) -> dict[str, Any]:
    """Write a new snapshot envelope, replacing any previous one wholesale."""
    envelope = {
        "snapshot_category": category,
        "version": int(version),
        "checksum": checksum_of(payload),
        "payload": payload,
    }
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(envelope, fh, sort_keys=True, indent=2)
            fh.write("\n")
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return envelope


def load_snapshot(path: str | Path, expected_category: str | None = None,
                  min_version: int | None = None) -> dict[str, Any]:
    target = Path(path)
    if not target.is_file():
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT", "snapshot",
            f"snapshot file missing: {target}",
            context={"snapshot_category": expected_category, "version": None},
        )
    try:
        envelope = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT", "snapshot",
            f"snapshot JSON is unreadable: {exc}",
            context={"snapshot_category": expected_category, "version": None},
        ) from exc

    category = envelope.get("snapshot_category")
    version = envelope.get("version")
    if expected_category is not None and category != expected_category:
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT", "snapshot",
            f"category mismatch: expected {expected_category}, got {category}",
            context={"snapshot_category": category, "version": version},
        )
    if min_version is not None and (not isinstance(version, int) or version < min_version):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT", "snapshot",
            f"version {version} is below required minimum {min_version}",
            context={"snapshot_category": category, "version": version},
        )
    payload = envelope.get("payload")
    actual = checksum_of(payload) if isinstance(payload, dict) else None
    if actual != envelope.get("checksum"):
        raise BusinessError(
            "E_SNAPSHOT_CORRUPT", "snapshot",
            "checksum verification failed; snapshot replaced wholesale is required",
            context={"snapshot_category": category, "version": version,
                     "expected_checksum": envelope.get("checksum"),
                     "actual_checksum": actual},
        )
    return envelope


def next_version(path: str | Path) -> int:
    """Return current version + 1, or 1 when no valid snapshot exists yet."""
    target = Path(path)
    if not target.is_file():
        return 1
    try:
        envelope = json.loads(target.read_text(encoding="utf-8"))
        current = int(envelope["version"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return 1
    return current + 1
