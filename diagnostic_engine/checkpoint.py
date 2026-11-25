"""Persistent block checkpoints and optimistic/non-blocking commit control."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from types import TracebackType
from typing import Any, Mapping

from .atomicio import atomic_write_json
from .errors import ErrorCode, DiagnosticError


class CommitLock:
    """Advisory flock that is released immediately on process termination."""

    def __init__(self, state_dir: str | Path):
        self.path = Path(state_dir) / "locks" / "commit.lock"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd: int | None = None

    def acquire_nonblocking(self) -> None:
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            os.close(fd)
            raise DiagnosticError(
                ErrorCode.STAGE_BUSY,
                "another block checkpoint is committing",
                field="commit_lock",
                details={"lock_path": str(self.path)},
            ) from error
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "CommitLock":
        self.acquire_nonblocking()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


class CheckpointStore:
    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self.dir = self.state_dir / "checkpoints"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = CommitLock(self.state_dir)

    def path_for(self, clip_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in ("-", "_", ".") else "_" for ch in clip_id)
        return self.dir / f"{safe}.json"

    def load(self, clip_id: str) -> dict[str, Any] | None:
        path = self.path_for(clip_id)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (json.JSONDecodeError, OSError) as error:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "checkpoint is unreadable",
                clip_id=clip_id,
                field="checkpoint",
                details={"path": str(path), "error": str(error)},
            ) from error
        if not isinstance(payload, dict) or payload.get("clip_id") != clip_id:
            raise DiagnosticError(
                ErrorCode.STATE_INCONSISTENT,
                "checkpoint clip_id does not match requested clip",
                clip_id=clip_id,
                field="clip_id",
                details={"path": str(path)},
            )
        return payload

    def current_version(self, clip_id: str) -> int:
        checkpoint = self.load(clip_id)
        return int(checkpoint["version"]) if checkpoint else 0

    def commit(
        self,
        clip_id: str,
        checkpoint: Mapping[str, Any],
        expected_version: int,
    ) -> dict[str, Any]:
        new_version = checkpoint.get("version")
        with self.lock:
            current = self.load(clip_id)
            actual_version = int(current["version"]) if current else 0
            if actual_version != expected_version:
                raise DiagnosticError(
                    ErrorCode.CHECKPOINT_CONFLICT,
                    "checkpoint version conflict; refusing stale overwrite",
                    clip_id=clip_id,
                    field="version",
                    details={
                        "expected_version": expected_version,
                        "actual_version": actual_version,
                        "attempted_version": new_version,
                    },
                )
            if new_version != actual_version + 1:
                raise DiagnosticError(
                    ErrorCode.CHECKPOINT_CONFLICT,
                    "new checkpoint version must be exactly one greater than current",
                    clip_id=clip_id,
                    field="version",
                    details={
                        "actual_version": actual_version,
                        "attempted_version": new_version,
                    },
                )
            atomic_write_json(self.path_for(clip_id), checkpoint)
        return dict(checkpoint)
