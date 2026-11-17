"""Versioned checkpoints with process-safe file locking."""

from __future__ import annotations

import os
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ErrorCode, SLPError
from .io_utils import (
    atomic_write_json,
    read_json_envelope,
    sha256_hex,
    canonical_json_bytes,
)
from .protocol import CHECKPOINT_SCHEMA_VERSION, PHASE_TABLE_VERSION


class FileLock(AbstractContextManager["FileLock"]):
    """Small cross-platform non-blocking advisory file lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if os.name == "nt":
                import msvcrt

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as error:
            os.close(descriptor)
            raise SLPError(
                ErrorCode.E_LOCK,
                f"checkpoint lock is already held: {self.path}",
            ) from error
        self._fd = descriptor
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        descriptor = self._fd
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.release()


@dataclass
class CheckpointState:
    completed: dict[str, dict[str, Any]] = field(default_factory=dict)
    rebuilt: bool = False
    rebuild_reason: str | None = None
    checkpoint_schema_version: int = CHECKPOINT_SCHEMA_VERSION
    phase_table_version: int = PHASE_TABLE_VERSION


def aggregate_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    success = sum(1 for result in results if result.get("status") == "ok")
    limited = sum(1 for result in results if result.get("status") == "limited")
    failed = len(results) - success - limited
    sessions = 0
    violations = 0
    matched = 0
    mismatched = 0
    unexpected = 0
    for result in results:
        session_list = result.get("sessions", [])
        sessions += len(session_list)
        violations += sum(len(session.get("violations", [])) for session in session_list)
        truth = result.get("truth", {})
        matched += int(truth.get("matched", 0))
        mismatched += int(truth.get("mismatched", 0))
        unexpected += int(truth.get("unexpected", 0))
    return {
        "files": len(results),
        "success": success,
        "limited": limited,
        "failed": failed,
        "sessions": sessions,
        "observed_violations": violations,
        "truth_matched_events": matched,
        "truth_mismatched_events": mismatched,
        "truth_unexpected_events": unexpected,
    }


class CheckpointStore:
    def __init__(
        self,
        workdir: str | Path,
        *,
        config_fingerprint: str = "",
        checkpoint_schema_version: int = CHECKPOINT_SCHEMA_VERSION,
        phase_table_version: int = PHASE_TABLE_VERSION,
    ) -> None:
        self.workdir = Path(workdir)
        self.directory = self.workdir / "checkpoints"
        self.path = self.directory / "checkpoint.json"
        self.lock_path = self.directory / ".checkpoint.lock"
        self.config_fingerprint = config_fingerprint
        self.checkpoint_schema_version = checkpoint_schema_version
        self.phase_table_version = phase_table_version

    def lock(self) -> FileLock:
        return FileLock(self.lock_path)

    def _empty(self, *, rebuilt: bool, reason: str | None) -> CheckpointState:
        return CheckpointState(
            completed={},
            rebuilt=rebuilt,
            rebuild_reason=reason,
            checkpoint_schema_version=self.checkpoint_schema_version,
            phase_table_version=self.phase_table_version,
        )

    def _load_unlocked(self) -> CheckpointState:
        if not self.path.exists():
            return self._empty(rebuilt=False, reason=None)
        payload = read_json_envelope(self.path, kind="slp-checkpoint")
        if not isinstance(payload, dict):
            raise SLPError(ErrorCode.E_CHECKPOINT, "checkpoint payload is not an object")
        actual_schema = int(payload.get("checkpoint_schema_version", -1))
        actual_phase = int(payload.get("phase_table_version", -1))
        if actual_schema != self.checkpoint_schema_version:
            return self._empty(
                rebuilt=True,
                reason=(
                    f"checkpoint schema changed: {actual_schema} -> "
                    f"{self.checkpoint_schema_version}"
                ),
            )
        if actual_phase != self.phase_table_version:
            return self._empty(
                rebuilt=True,
                reason=(
                    f"phase table changed: {actual_phase} -> "
                    f"{self.phase_table_version}"
                ),
            )
        if payload.get("config_fingerprint") != self.config_fingerprint:
            return self._empty(
                rebuilt=True,
                reason="analysis configuration changed; checkpoint rebuilt",
            )
        completed = payload.get("completed", {})
        if not isinstance(completed, dict):
            raise SLPError(ErrorCode.E_CHECKPOINT, "checkpoint completed map is invalid")
        clean: dict[str, dict[str, Any]] = {}
        for file_id, result in completed.items():
            if not isinstance(file_id, str) or not isinstance(result, dict):
                raise SLPError(
                    ErrorCode.E_CHECKPOINT,
                    "checkpoint contains a malformed completed entry",
                )
            if result.get("file_id") != file_id:
                raise SLPError(
                    ErrorCode.E_CHECKPOINT,
                    f"checkpoint entry {file_id} has a mismatched file_id",
                )
            clean[file_id] = result
        return CheckpointState(
            completed=clean,
            rebuilt=False,
            checkpoint_schema_version=actual_schema,
            phase_table_version=actual_phase,
        )

    def _save_unlocked(self, state: CheckpointState) -> None:
        payload = {
            "checkpoint_schema_version": self.checkpoint_schema_version,
            "phase_table_version": self.phase_table_version,
            "config_fingerprint": self.config_fingerprint,
            "completed": state.completed,
            "aggregate": aggregate_results(list(state.completed.values())),
        }
        atomic_write_json(self.path, payload, kind="slp-checkpoint")

    def load(self) -> CheckpointState:
        with self.lock():
            return self._load_unlocked()

    def save(self, state: CheckpointState) -> None:
        with self.lock():
            self._save_unlocked(state)

    def record_file(self, file_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Atomically merge one completed file into the last checkpoint."""

        with self.lock():
            state = self._load_unlocked()
            state.completed[file_id] = result
            self._save_unlocked(state)
        return aggregate_results(list(state.completed.values()))

    def replace_current(self, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Prune stale IDs and persist the current batch's completed map."""

        with self.lock():
            state = CheckpointState(
                completed=dict(results),
                checkpoint_schema_version=self.checkpoint_schema_version,
                phase_table_version=self.phase_table_version,
            )
            self._save_unlocked(state)
        return aggregate_results(list(results.values()))

    def current_digest(self) -> str | None:
        if not self.path.exists():
            return None
        with self.lock():
            payload = read_json_envelope(self.path, kind="slp-checkpoint")
        return sha256_hex(canonical_json_bytes(payload))
