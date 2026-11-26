"""Non-blocking staged commit control with full-snapshot replacement."""

from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .canonical import atomic_write_json, read_json_strict_no_duplicates, sha256_json
from .errors import (
    E_LEDGER_CONFLICT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    WorkbenchError,
    corrupt,
)


@dataclass(frozen=True)
class LedgerState:
    version: int
    checksum: str | None
    kind: str | None
    snapshot: Mapping[str, Any] | None
    envelope: Mapping[str, Any] | None


@dataclass(frozen=True)
class LedgerCommit:
    version: int
    checksum: str
    full_snapshot_replace: bool
    drift_detected: bool
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "full_snapshot_replace": self.full_snapshot_replace,
            "drift_detected": self.drift_detected,
            "idempotent": self.idempotent,
        }


class StageLock:
    """A single non-blocking lock shared by config and checkpoint commits."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def hold(self) -> Iterator[None]:
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkbenchError(
                    E_STAGE_BUSY,
                    "another configuration or checkpoint commit is active",
                    {"lock_path": str(self.path)},
                ) from exc
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


class CommitLedger:
    def __init__(self, root: str | Path, *, lock: StageLock | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "ledger.json"
        self.lock = lock or StageLock(self.root / "stage.lock")

    def read(self) -> LedgerState:
        if not self.path.exists():
            return LedgerState(0, None, None, None, None)
        envelope = read_json_strict_no_duplicates(self.path)
        if not isinstance(envelope, dict):
            raise corrupt("ledger envelope must be a JSON object")
        required = {
            "schema_version",
            "kind",
            "version",
            "parent_version",
            "snapshot",
            "snapshot_checksum",
            "content_checksum",
        }
        missing = sorted(required.difference(envelope))
        if missing:
            raise corrupt("ledger envelope is incomplete", missing=missing)
        checksum = envelope.get("content_checksum")
        body = {key: value for key, value in envelope.items() if key != "content_checksum"}
        actual = sha256_json(body)
        if checksum != actual:
            raise corrupt(
                "ledger checksum mismatch",
                expected=checksum,
                actual=actual,
            )
        snapshot = envelope.get("snapshot")
        if not isinstance(snapshot, dict):
            raise corrupt("ledger snapshot must be a JSON object")
        snapshot_checksum = envelope.get("snapshot_checksum")
        if snapshot_checksum != sha256_json(snapshot):
            raise corrupt("ledger snapshot checksum mismatch")
        version = envelope.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise corrupt("ledger version is invalid", version=version)
        return LedgerState(
            version=version,
            checksum=str(checksum),
            kind=str(envelope.get("kind")),
            snapshot=snapshot,
            envelope=envelope,
        )

    def commit(
        self,
        *,
        kind: str,
        snapshot: Mapping[str, Any],
        version: int,
        expected_parent_version: int | None = None,
        expected_parent_checksum: str | None = None,
    ) -> LedgerCommit:
        if not kind:
            raise corrupt("ledger commit kind must be non-empty")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise corrupt("ledger commit version must be a positive integer", version=version)
        snapshot_dict = dict(snapshot)
        snapshot_checksum = sha256_json(snapshot_dict)
        with self.lock.hold():
            current = self.read()
            if version < current.version:
                raise WorkbenchError(
                    E_SNAPSHOT_ROLLBACK,
                    "ledger version rollback is forbidden",
                    {
                        "requested_version": version,
                        "current_version": current.version,
                    },
                )
            if version == current.version:
                if (
                    current.kind == kind
                    and current.snapshot is not None
                    and sha256_json(dict(current.snapshot)) == snapshot_checksum
                ):
                    return LedgerCommit(
                        version=version,
                        checksum=str(current.checksum),
                        full_snapshot_replace=False,
                        drift_detected=False,
                        idempotent=True,
                    )
                raise WorkbenchError(
                    E_LEDGER_CONFLICT,
                    "same ledger version cannot contain different content",
                    {
                        "version": version,
                        "current_checksum": current.checksum,
                        "requested_snapshot_checksum": snapshot_checksum,
                    },
                )
            drift = (
                (
                    expected_parent_version is not None
                    and expected_parent_version != current.version
                )
                or (
                    expected_parent_checksum is not None
                    and expected_parent_checksum != current.checksum
                )
            )
            body = {
                "schema_version": 1,
                "kind": kind,
                "version": version,
                "parent_version": current.version,
                "requested_parent_version": expected_parent_version,
                "drift_detected": drift,
                "snapshot": snapshot_dict,
                "snapshot_checksum": snapshot_checksum,
            }
            envelope = dict(body)
            envelope["content_checksum"] = sha256_json(body)
            atomic_write_json(self.path, envelope)
            return LedgerCommit(
                version=version,
                checksum=envelope["content_checksum"],
                full_snapshot_replace=True,
                drift_detected=drift,
            )

