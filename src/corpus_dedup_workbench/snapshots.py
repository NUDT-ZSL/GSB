from __future__ import annotations

import copy
from dataclasses import dataclass
from threading import RLock
from typing import Any

from .errors import (
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    WorkbenchError,
)
from .util import content_hash


@dataclass(frozen=True)
class SnapshotRecord:
    kind: str
    version: int
    checksum: str
    payload: dict[str, Any]


class SnapshotManager:
    """Monotonic, checksummed, whole-object snapshot replacement."""

    def __init__(self) -> None:
        self._records: dict[str, SnapshotRecord] = {}
        self._lock = RLock()

    def current_version(self, kind: str) -> int:
        with self._lock:
            record = self._records.get(kind)
            return record.version if record else 0

    def publish(
        self,
        kind: str,
        payload: dict[str, Any],
        version: int | None = None,
        expected_checksum: str | None = None,
    ) -> SnapshotRecord:
        with self._lock:
            current = self._records.get(kind)
            next_version = (current.version + 1) if current else 1
            selected_version = next_version if version is None else int(version)
            checksum = content_hash(payload)
            if expected_checksum is not None and expected_checksum != checksum:
                raise WorkbenchError(
                    E_SNAPSHOT_CORRUPT,
                    "快照校验和不符",
                    stage="snapshot_publish",
                    details={
                        "kind": kind,
                        "version": selected_version,
                        "expected": expected_checksum,
                        "actual": checksum,
                    },
                )
            if current and selected_version <= current.version:
                raise WorkbenchError(
                    E_SNAPSHOT_ROLLBACK,
                    "快照版本必须单调递增，禁止回退或同版本覆盖",
                    stage="snapshot_publish",
                    details={
                        "kind": kind,
                        "current_version": current.version,
                        "attempted_version": selected_version,
                    },
                )
            frozen_payload = copy.deepcopy(payload)
            record = SnapshotRecord(
                kind=kind,
                version=selected_version,
                checksum=checksum,
                payload=frozen_payload,
            )
            self._records[kind] = record
            return record

    def get(self, kind: str, verify: bool = True) -> dict[str, Any]:
        with self._lock:
            record = self._records.get(kind)
            if record is None:
                raise WorkbenchError(
                    E_SNAPSHOT_CORRUPT,
                    "快照不存在",
                    stage="snapshot_read",
                    details={"kind": kind},
                )
            if verify:
                actual = content_hash(record.payload)
                if actual != record.checksum:
                    raise WorkbenchError(
                        E_SNAPSHOT_CORRUPT,
                        "快照内容校验和不符",
                        stage="snapshot_read",
                        details={
                            "kind": kind,
                            "version": record.version,
                            "expected": record.checksum,
                            "actual": actual,
                        },
                    )
            return {
                "kind": record.kind,
                "version": record.version,
                "checksum": record.checksum,
                "payload": copy.deepcopy(record.payload),
            }

    def metadata(self) -> dict[str, Any]:
        with self._lock:
            return {
                kind: {
                    "version": record.version,
                    "checksum": record.checksum,
                }
                for kind, record in sorted(self._records.items())
            }

    def _tamper_for_test(self, kind: str, payload: dict[str, Any]) -> None:
        """Test hook: rewrite the frozen payload without updating its checksum."""
        with self._lock:
            record = self._records[kind]
            self._records[kind] = SnapshotRecord(
                kind=record.kind,
                version=record.version,
                checksum=record.checksum,
                payload=copy.deepcopy(payload),
            )

