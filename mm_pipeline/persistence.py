"""Atomic file replacement, round snapshots and version-hash verification."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SNAPSHOT_NAME = "snapshot.json"
E_CONFIG_MISMATCH = "E_CONFIG_MISMATCH"


class SnapshotError(RuntimeError):
    """Raised when snapshot content is unreadable or version-mismatched."""


def atomic_write_json(path: str | Path, payload: dict) -> None:
    """Write JSON via a same-volume temp file then atomic os.replace."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.",
                                    suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2,
                      sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
        target_dir_fd = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(target_dir_fd)
        finally:
            os.close(target_dir_fd)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def cleanup_stale_temp(snapshot_dir: str | Path) -> None:
    snapshot_dir = Path(snapshot_dir)
    if not snapshot_dir.exists():
        return
    for stale in snapshot_dir.glob(f".{SNAPSHOT_NAME}.*.tmp"):
        try:
            stale.unlink()
        except OSError:
            pass


def snapshot_path(snapshot_dir: str | Path) -> Path:
    return Path(snapshot_dir) / SNAPSHOT_NAME


def write_snapshot(snapshot_dir: str | Path, snapshot: dict) -> dict:
    path = snapshot_path(snapshot_dir)
    atomic_write_json(path, snapshot)
    return snapshot


def load_snapshot(snapshot_dir: str | Path):
    path = snapshot_path(snapshot_dir)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"snapshot unreadable: {exc}") from exc


def verify_snapshot(snapshot: dict, config_hash: str,
                    relations_hash: str) -> None:
    """Refuse recovery when config or relation-table version has changed."""
    mismatch_fields = []
    if snapshot.get("config_hash") != config_hash:
        mismatch_fields.append("config_hash")
    if snapshot.get("relations_hash") != relations_hash:
        mismatch_fields.append("relations_hash")
    if mismatch_fields:
        raise SnapshotError(json.dumps({
            "code": E_CONFIG_MISMATCH,
            "mismatch_fields": mismatch_fields,
            "snapshot_config_hash": snapshot.get("config_hash"),
            "current_config_hash": config_hash,
            "snapshot_relations_hash": snapshot.get("relations_hash"),
            "current_relations_hash": relations_hash,
        }, ensure_ascii=False))
