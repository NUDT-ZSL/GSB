"""Atomic persistent task state."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import BusinessError


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None
    except (OSError, ValueError, TypeError):
        return None


class StateStore:
    """One state file per output; every transition is an atomic replacement."""

    def __init__(self, path: Path, snapshot_id: str) -> None:
        self.path = path
        self.snapshot_id = snapshot_id
        self.data: dict[str, Any] = {}

    def initialise(
        self,
        *,
        output: dict[str, Any],
        completed_tracks: list[dict[str, Any]] | None = None,
        recovery: dict[str, Any] | None = None,
        preserve_started_at: bool = True,
    ) -> None:
        previous = read_json(self.path)
        started_at = utc_now()
        if (
            preserve_started_at
            and previous
            and isinstance(previous.get("started_at"), str)
        ):
            started_at = previous["started_at"]
        self.data = {
            "schema_version": 1,
            "snapshot_id": self.snapshot_id,
            "stage": "prepared",
            "completed_tracks": completed_tracks or [],
            "output": output,
            "recovery": recovery or {},
            "started_at": started_at,
            "updated_at": utc_now(),
        }
        self._write()

    def set_stage(
        self,
        stage: str,
        *,
        completed_tracks: list[dict[str, Any]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.data["stage"] = stage
        if completed_tracks is not None:
            self.data["completed_tracks"] = completed_tracks
        if extra:
            self.data.update(extra)
        self.data["updated_at"] = utc_now()
        self._write()

    def add_completed_track(self, entry: dict[str, Any]) -> None:
        tracks = list(self.data.get("completed_tracks", []))
        by_index = {item.get("index"): item for item in tracks if isinstance(item, dict)}
        by_index[int(entry["index"])] = entry
        tracks = [by_index[key] for key in sorted(by_index)]
        self.data["completed_tracks"] = tracks
        self.data["updated_at"] = utc_now()
        self._write()

    def mark_interrupted(self, interrupted_stage: str, reason: str) -> None:
        self.data["stage"] = "interrupted"
        self.data["interrupted_stage"] = interrupted_stage
        self.data["interrupt_reason"] = reason
        self.data["updated_at"] = utc_now()
        self._write()

    def mark_failed(self, failed_stage: str, error: BusinessError) -> None:
        self.data["stage"] = "failed"
        self.data["failed_stage"] = failed_stage
        self.data["error"] = error.to_dict()["error"]
        self.data["updated_at"] = utc_now()
        try:
            self._write()
        except OSError:
            pass

    def _write(self) -> None:
        try:
            atomic_write_json(self.path, self.data)
        except OSError as exc:
            raise BusinessError(
                "OUTPUT_WRITE_FAILED",
                "无法写入持久化任务状态",
                {"state_path": str(self.path), "reason": str(exc)},
            ) from exc
