"""Atomic stage checkpoints with explicit savepoint recovery."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable

from .canonical import (
    atomic_write_json,
    read_json,
    sha256_value,
    verify_payload_checksum,
)
from .errors import DriftError, E_CHECKPOINT_CORRUPT
from .locking import FileCommitLock, resolve_commit_lock


STAGES = (
    "ingest",
    "align",
    "score",
    "retrain",
    "ledger",
    "exit_report",
    "done",
)
CHECKPOINT_SCHEMA = "stage-checkpoint-v1"


class StageStateMachine:
    """Persists only fully committed stages.

    A signal may move the machine into ``exit_report``.  The checkpoint at that
    point is a durable boundary; callers can either resume the exit report or
    create/restore a named savepoint explicitly.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        lock_path: str | Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.lock_path = resolve_commit_lock(lock_path)

    def initial_state(self) -> dict[str, Any]:
        body = {
            "schema": CHECKPOINT_SCHEMA,
            "version": 0,
            "stage": "new",
            "state": "running",
            "completed_stages": [],
            "completed_records": [],
            "exit_requested": False,
            "pending_exit": None,
            "restored_from": None,
        }
        body["checksum"] = sha256_value(body)
        return body

    def _validate(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("schema") != CHECKPOINT_SCHEMA:
            raise DriftError(
                E_CHECKPOINT_CORRUPT, "checkpoint schema mismatch"
            )
        try:
            version = int(payload["version"])
            completed_stages = [str(item) for item in payload["completed_stages"]]
            completed_records = [str(item) for item in payload["completed_records"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise DriftError(
                E_CHECKPOINT_CORRUPT, "checkpoint fields are invalid"
            ) from exc
        if version < 0:
            raise DriftError(E_CHECKPOINT_CORRUPT, "checkpoint version is negative")
        if len(set(completed_stages)) != len(completed_stages):
            raise DriftError(
                E_CHECKPOINT_CORRUPT, "checkpoint has duplicate completed stages"
            )
        if not set(completed_stages) <= set(STAGES):
            raise DriftError(
                E_CHECKPOINT_CORRUPT, "checkpoint has unknown completed stages"
            )
        expected_order = [
            stage for stage in STAGES if stage in set(completed_stages)
        ]
        if completed_stages != expected_order:
            raise DriftError(
                E_CHECKPOINT_CORRUPT,
                "completed stage order is not canonical",
            )
        try:
            verify_payload_checksum(payload)
        except DriftError as exc:
            raise DriftError(
                E_CHECKPOINT_CORRUPT,
                "checkpoint content checksum mismatch",
            ) from exc
        normalized = dict(payload)
        normalized["version"] = version
        normalized["completed_stages"] = completed_stages
        normalized["completed_records"] = sorted(set(completed_records))
        return normalized

    def resume(self) -> dict[str, Any]:
        if not self.path.exists():
            return self.initial_state()
        try:
            payload = read_json(self.path)
        except DriftError as exc:
            raise DriftError(
                E_CHECKPOINT_CORRUPT, "cannot read checkpoint"
            ) from exc
        state = self._validate(payload)
        state["resume_action"] = (
            "finish_exit_report" if state.get("exit_requested") else "continue"
        )
        return state

    def commit_stage(
        self,
        stage: str,
        *,
        records: Iterable[str] = (),
        state: str = "running",
        exit_requested: bool = False,
        pending_exit: str | None = None,
    ) -> dict[str, Any]:
        if stage not in STAGES:
            raise DriftError(
                E_CHECKPOINT_CORRUPT, f"unknown stage {stage}"
            )
        with FileCommitLock(self.lock_path):
            current = self.resume()
            next_version = int(current["version"]) + 1
            completed_stages = set(current["completed_stages"])
            if stage in completed_stages:
                completed_stages.remove(stage)
            completed_stages.add(stage)
            ordered_stages = [
                item for item in STAGES if item in completed_stages
            ]
            completed_records = set(current["completed_records"])
            completed_records.update(str(item) for item in records)
            body = {
                "schema": CHECKPOINT_SCHEMA,
                "version": next_version,
                "stage": stage,
                "state": state,
                "completed_stages": ordered_stages,
                "completed_records": sorted(completed_records),
                "exit_requested": bool(exit_requested),
                "pending_exit": pending_exit,
                "restored_from": current.get("restored_from"),
                "committed_at_ms": int(time.time() * 1000),
            }
            body["checksum"] = sha256_value(body)
            atomic_write_json(self.path, body)
            return body

    def enter_exit_report(self, signal_name: str) -> dict[str, Any]:
        return self.commit_stage(
            "exit_report",
            records=(f"signal:{signal_name}",),
            state="exit_report",
            exit_requested=True,
            pending_exit=signal_name,
        )

    def acknowledge_exit(self) -> dict[str, Any]:
        current = self.resume()
        return self.commit_stage(
            "done",
            records=(f"exit:{current.get('pending_exit') or 'normal'}",),
            state="done",
            exit_requested=False,
            pending_exit=None,
        )

    def _savepoint_path(self, label: str) -> Path:
        safe = "".join(
            character if character.isalnum() or character in "-_" else "_"
            for character in label
        )
        digest = sha256_value(label)[:12]
        return self.path.parent / "savepoints" / f"{safe}-{digest}.json"

    def savepoint(self, label: str) -> dict[str, Any]:
        with FileCommitLock(self.lock_path):
            current = self.resume()
            target = self._savepoint_path(label)
            persisted = {
                key: value
                for key, value in current.items()
                if key != "resume_action"
            }
            atomic_write_json(target, persisted)
            return {
                "label": label,
                "path": str(target),
                "checkpoint": persisted,
            }

    def restore_savepoint(self, label: str) -> dict[str, Any]:
        target = self._savepoint_path(label)
        if not target.exists():
            raise DriftError(
                E_CHECKPOINT_CORRUPT,
                "explicit savepoint is missing",
                label=label,
                path=str(target),
            )
        saved = self._validate(read_json(target))
        with FileCommitLock(self.lock_path):
            current = self.resume()
            body = {
                key: value
                for key, value in saved.items()
                if key not in {"checksum", "resume_action", "committed_at_ms"}
            }
            body["version"] = max(
                int(current["version"]), int(saved["version"])
            ) + 1
            body["restored_from"] = label
            body["state"] = "running"
            body["committed_at_ms"] = int(time.time() * 1000)
            body["checksum"] = sha256_value(body)
            atomic_write_json(self.path, body)
            return body


def checkpoint_error_replay(path: str | Path) -> dict[str, Any]:
    try:
        state = StageStateMachine(path).resume()
        return {"ok": True, **state}
    except DriftError as exc:
        return {"ok": False, **exc.to_dict()}

