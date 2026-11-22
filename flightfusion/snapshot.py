"""Versioned whole-snapshot replacement with non-blocking commit control."""

from __future__ import annotations

import copy
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from .errors import BusinessError, ErrorCode
from .serialization import clone_json, content_hash


@dataclass(frozen=True)
class Snapshot:
    version: int
    checksum: str
    payload: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "payload": clone_json(self.payload),
        }


@dataclass(frozen=True)
class CommitResult:
    status: str
    snapshot: Snapshot
    previous_version: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "snapshot": self.snapshot.as_dict(),
            "previous_version": self.previous_version,
        }


class SnapshotStore:
    """Holds immutable whole snapshots.

    Commits never merge into the active payload. The critical section is
    non-blocking: a competing submitter receives ``E_STAGE_BUSY`` immediately.
    """

    def __init__(self, *, commit_delay_s: float = 0.0):
        self._lock = threading.Lock()
        self._current: Snapshot | None = None
        self._history: list[Snapshot] = []
        self._commit_delay_s = max(0.0, commit_delay_s)
        self.last_error_code: str | None = None

    @property
    def current(self) -> Snapshot | None:
        current = self._current
        if current is None:
            return None
        return Snapshot(
            version=current.version,
            checksum=current.checksum,
            payload=copy.deepcopy(current.payload),
        )

    @property
    def history(self) -> list[Snapshot]:
        return [
            Snapshot(
                version=item.version,
                checksum=item.checksum,
                payload=copy.deepcopy(item.payload),
            )
            for item in self._history
        ]

    def _busy(self) -> BusinessError:
        error = BusinessError(
            code=ErrorCode.STAGE_BUSY,
            message="another incremental snapshot commit is active",
            stage="snapshot",
            field="commit_lock",
            degradation="reject_without_waiting",
        )
        self.last_error_code = error.code
        return error

    def commit(
        self,
        payload: dict[str, Any],
        *,
        version: int,
        provided_checksum: str | None = None,
    ) -> CommitResult:
        if version < 1:
            error = BusinessError(
                code=ErrorCode.SNAPSHOT_ROLLBACK,
                message="snapshot version must be >= 1",
                stage="snapshot",
                field="version",
                value=version,
            )
            self.last_error_code = error.code
            raise error
        if not self._lock.acquire(blocking=False):
            raise self._busy()
        try:
            if self._commit_delay_s:
                time.sleep(self._commit_delay_s)
            payload_copy = clone_json(payload)
            checksum = content_hash(payload_copy)
            if provided_checksum is not None and provided_checksum != checksum:
                error = BusinessError(
                    code=ErrorCode.SNAPSHOT_CORRUPT,
                    message="snapshot checksum does not match payload",
                    stage="snapshot",
                    field="checksum",
                    value={
                        "provided": provided_checksum,
                        "computed": checksum,
                    },
                )
                self.last_error_code = error.code
                raise error

            previous = self._current
            if previous is not None:
                if version < previous.version:
                    error = BusinessError(
                        code=ErrorCode.SNAPSHOT_ROLLBACK,
                        message=(
                            f"snapshot v{version} cannot replace active "
                            f"v{previous.version}"
                        ),
                        stage="snapshot",
                        field="version",
                        value=version,
                        details={"active_version": previous.version},
                    )
                    self.last_error_code = error.code
                    raise error
                if version == previous.version:
                    if checksum == previous.checksum:
                        return CommitResult(
                            status="unchanged",
                            snapshot=Snapshot(
                                previous.version,
                                previous.checksum,
                                copy.deepcopy(previous.payload),
                            ),
                            previous_version=previous.version,
                        )
                    error = BusinessError(
                        code=ErrorCode.SNAPSHOT_ROLLBACK,
                        message=(
                            f"snapshot v{version} already exists with a different "
                            "checksum; in-place mutation is forbidden"
                        ),
                        stage="snapshot",
                        field="checksum",
                        value=checksum,
                        details={
                            "active_checksum": previous.checksum,
                            "submitted_checksum": checksum,
                        },
                    )
                    self.last_error_code = error.code
                    raise error

            snapshot = Snapshot(version=version, checksum=checksum, payload=payload_copy)
            self._current = snapshot
            self._history.append(snapshot)
            self.last_error_code = None
            return CommitResult(
                status="replaced" if previous is not None else "created",
                snapshot=snapshot,
                previous_version=None if previous is None else previous.version,
            )
        finally:
            self._lock.release()

    @contextmanager
    def hold_commit_lock(self) -> Iterator[None]:
        """Test/diagnostic hook that holds the same non-blocking commit lock."""

        if not self._lock.acquire(blocking=False):
            raise self._busy()
        try:
            yield
        finally:
            self._lock.release()

    def verify(self, snapshot: dict[str, Any]) -> bool:
        try:
            payload = snapshot["payload"]
            checksum = str(snapshot["checksum"])
            version = int(snapshot["version"])
        except (KeyError, TypeError, ValueError) as exc:
            error = BusinessError(
                code=ErrorCode.SNAPSHOT_CORRUPT,
                message="snapshot envelope is incomplete",
                stage="snapshot",
                field="snapshot",
                value=snapshot,
            )
            self.last_error_code = error.code
            raise error from exc
        actual = content_hash(payload)
        if actual != checksum:
            error = BusinessError(
                code=ErrorCode.SNAPSHOT_CORRUPT,
                message="snapshot checksum verification failed",
                stage="snapshot",
                field="checksum",
                value={"provided": checksum, "computed": actual},
                details={"version": version},
            )
            self.last_error_code = error.code
            raise error
        return True

