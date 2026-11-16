"""Atomic JSON persistence and content hashing (requirement 7)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def stable_hash(obj) -> str:
    """SHA-256 over canonical JSON (sorted keys, compact separators)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def atomic_write_json(path: Path, obj) -> None:
    """Write to a temp file in the same directory, then os.replace."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def save_snapshot(path: Path, **fields) -> None:
    atomic_write_json(path, fields)


def load_snapshot(path: Path):
    """Return the snapshot dict, or None if absent or not fully replaced
    (corrupt/truncated snapshots are treated as if no snapshot exists)."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
