"""Structured business errors and persistent latest-error reporting."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class ErrorCode:
    INPUT_INVALID = "E_INPUT_INVALID"
    JSONL_INVALID = "E_JSONL_INVALID"
    DUPLICATE_CLIP_ID = "E_DUPLICATE_CLIP_ID"
    NOISE_SHAPE_MISMATCH = "E_NOISE_SHAPE_MISMATCH"
    PROPAGATION_INVALID = "E_PROPAGATION_INVALID"
    REVISION_ROLLBACK = "E_REVISION_ROLLBACK"
    CHECKPOINT_CONFLICT = "E_CHECKPOINT_CONFLICT"
    RECOVERY_MISMATCH = "E_RECOVERY_MISMATCH"
    CACHE_KEY_INVALID = "E_CACHE_KEY_INVALID"
    INVALIDATION_MISMATCH = "E_INVALIDATION_MISMATCH"
    STATE_INCONSISTENT = "E_STATE_INCONSISTENT"
    STAGE_BUSY = "E_STAGE_BUSY"
    RESOURCE_LIMIT = "E_RESOURCE_LIMIT"


@dataclass
class DiagnosticError(Exception):
    code: str
    message: str
    clip_id: str | None = None
    field: str | None = None
    details: dict[str, Any] | None = None

    def __str__(self) -> str:
        prefix = f"{self.code}: "
        location = []
        if self.clip_id is not None:
            location.append(f"clip_id={self.clip_id}")
        if self.field is not None:
            location.append(f"field={self.field}")
        if location:
            return prefix + self.message + " (" + ", ".join(location) + ")"
        return prefix + self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "clip_id": self.clip_id,
            "field": self.field,
            "details": self.details or {},
        }


def error_from_dict(data: Mapping[str, Any]) -> DiagnosticError:
    return DiagnosticError(
        code=str(data.get("code", ErrorCode.INPUT_INVALID)),
        message=str(data.get("message", "unknown diagnostic error")),
        clip_id=data.get("clip_id"),
        field=data.get("field"),
        details=dict(data.get("details") or {}),
    )


class ErrorJournal:
    """Append business errors and retain the most recent one atomically."""

    def __init__(self, state_dir: str | os.PathLike[str]):
        self.dir = Path(state_dir) / "errors"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.latest_path = self.dir / "latest_error.json"
        self.log_path = self.dir / "error_log.jsonl"

    def record(self, error: DiagnosticError | Mapping[str, Any]) -> dict[str, Any]:
        payload = error.to_dict() if isinstance(error, DiagnosticError) else dict(error)
        _atomic_write_json(self.latest_path, payload)
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n")
        return payload

    def latest(self) -> dict[str, Any] | None:
        if not self.latest_path.exists():
            return None
        with self.latest_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
