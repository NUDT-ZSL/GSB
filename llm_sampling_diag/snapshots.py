from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .canonical import sha256_json
from .errors import DiagnosticError, ErrorCode


@dataclass
class Snapshot:
    kind: str
    version: int
    checksum: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "checksum": self.checksum,
            "payload": copy.deepcopy(self.payload),
        }


@dataclass
class StageState:
    lock: threading.Lock = field(default_factory=threading.Lock)
    current: Snapshot | None = None
    history: list[Snapshot] = field(default_factory=list)
    conflict_count: int = 0
    busy_count: int = 0


class SnapshotManager:
    def __init__(self, *, hook: Callable[[str], None] | None = None) -> None:
        self._stages: dict[str, StageState] = {}
        self._registry_lock = threading.Lock()
        self._hook = hook

    def _state(self, kind: str) -> StageState:
        with self._registry_lock:
            if kind not in self._stages:
                self._stages[kind] = StageState()
            return self._stages[kind]

    def commit(
        self,
        kind: str,
        version: int,
        payload: dict[str, Any],
        *,
        checksum: str | None = None,
        commit_delay_s: float = 0.0,
    ) -> Snapshot:
        if kind not in {"task", "parameter", "result"}:
            raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "unknown snapshot stage", field="kind")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise DiagnosticError(ErrorCode.E_CONFIG_INVALID, "snapshot version must be >= 1", field="version")
        computed_checksum = sha256_json(payload)
        if checksum is not None and checksum != computed_checksum:
            raise DiagnosticError(
                ErrorCode.E_SNAPSHOT_CHECKSUM_MISMATCH,
                "snapshot checksum does not match canonical payload",
                field="checksum",
                details={"expected": computed_checksum, "provided": checksum},
            )
        state = self._state(kind)
        if not state.lock.acquire(blocking=False):
            state.busy_count += 1
            raise DiagnosticError(
                ErrorCode.E_STAGE_BUSY,
                f"stage {kind!r} is busy with another snapshot commit",
                field="kind",
                details={"stage": kind, "version": version},
            )
        try:
            if self._hook is not None:
                self._hook(kind)
            if commit_delay_s > 0:
                time.sleep(commit_delay_s)
            current = state.current
            if current is not None:
                if version < current.version:
                    state.conflict_count += 1
                    raise DiagnosticError(
                        ErrorCode.E_SNAPSHOT_ROLLBACK,
                        "snapshot version would roll back a newer snapshot",
                        field="version",
                        details={
                            "stage": kind,
                            "current_version": current.version,
                            "attempted_version": version,
                            "current_checksum": current.checksum,
                        },
                    )
                if version == current.version and computed_checksum != current.checksum:
                    state.conflict_count += 1
                    raise DiagnosticError(
                        ErrorCode.E_SNAPSHOT_CONFLICT,
                        "snapshot version already exists with different content",
                        field="checksum",
                        details={
                            "stage": kind,
                            "version": version,
                            "current_checksum": current.checksum,
                            "attempted_checksum": computed_checksum,
                        },
                    )
                if version == current.version and computed_checksum == current.checksum:
                    return Snapshot(
                        kind=kind,
                        version=current.version,
                        checksum=current.checksum,
                        payload=copy.deepcopy(current.payload),
                    )
            snapshot = Snapshot(
                kind=kind,
                version=version,
                checksum=computed_checksum,
                payload=copy.deepcopy(payload),
            )
            state.current = snapshot
            state.history.append(
                Snapshot(
                    kind=snapshot.kind,
                    version=snapshot.version,
                    checksum=snapshot.checksum,
                    payload=copy.deepcopy(snapshot.payload),
                )
            )
            return Snapshot(
                kind=snapshot.kind,
                version=snapshot.version,
                checksum=snapshot.checksum,
                payload=copy.deepcopy(snapshot.payload),
            )
        finally:
            state.lock.release()

    def get(self, kind: str) -> Snapshot | None:
        state = self._state(kind)
        if not state.lock.acquire(blocking=False):
            raise DiagnosticError(
                ErrorCode.E_STAGE_BUSY,
                f"stage {kind!r} is busy with a snapshot commit",
                field="kind",
            )
        try:
            current = state.current
            if current is None:
                return None
            return Snapshot(
                kind=current.kind,
                version=current.version,
                checksum=current.checksum,
                payload=copy.deepcopy(current.payload),
            )
        finally:
            state.lock.release()

    def history(self, kind: str) -> list[dict[str, Any]]:
        state = self._state(kind)
        return [snapshot.to_dict() for snapshot in state.history]

    def commit_bundle(
        self,
        *,
        version: int,
        task_payload: dict[str, Any],
        parameter_payload: dict[str, Any],
        result_payload: dict[str, Any],
    ) -> dict[str, Any]:
        snapshots = {}
        for kind, payload in (
            ("task", task_payload),
            ("parameter", parameter_payload),
            ("result", result_payload),
        ):
            snapshots[kind] = self.commit(kind, version, payload).to_dict()
        return {
            "version": version,
            "snapshots": snapshots,
            "transaction_checksum": sha256_json(
                {
                    kind: snapshot["checksum"] for kind, snapshot in snapshots.items()
                }
            ),
        }

