"""Whole-replacement ledger/config snapshots and last-business-code journal."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .errors import (
    DiagnosticError,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
)
from .locking import NonBlockingFileLock
from .util import (
    atomic_write_json,
    read_json,
    seal_checksum,
    sha256_json,
)


@dataclass(frozen=True)
class SnapshotView:
    version: int
    body: dict[str, Any]
    checksum: str


class VersionedSnapshotStore:
    """A whole-document store with non-blocking single-writer semantics."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        format_name: str,
        lock_path: str | os.PathLike[str] | None = None,
    ):
        self.path = Path(path)
        self.format_name = format_name
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.path.with_name(self.path.name + ".lock")
        )

    def load(self) -> SnapshotView | None:
        try:
            envelope = read_json(self.path)
        except FileNotFoundError:
            return None
        except DiagnosticError:
            raise
        if not isinstance(envelope, dict):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot root must be an object",
                {"path": str(self.path)},
            )
        try:
            version = int(envelope["version"])
            body = envelope["body"]
            checksum = str(envelope["checksum"])
        except (TypeError, ValueError, KeyError) as exc:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot envelope metadata is invalid",
                {"path": str(self.path), "error": str(exc)},
            ) from exc
        if version <= 0:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot version must be positive",
                {"path": str(self.path)},
            )
        if not isinstance(body, dict):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot body must be an object",
                {"path": str(self.path)},
            )
        if envelope.get("format") != self.format_name:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot format marker is invalid",
                {"path": str(self.path), "format": envelope.get("format")},
            )
        expected_checksum = self._envelope_checksum(version, body)
        if checksum != expected_checksum:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "snapshot checksum mismatch",
                {
                    "path": str(self.path),
                    "expected": checksum,
                    "actual": expected_checksum,
                },
            )
        return SnapshotView(version=version, body=body, checksum=checksum)

    def _envelope_checksum(self, version: int, body: Mapping[str, Any]) -> str:
        return sha256_json(
            {
                "body": dict(body),
                "format": self.format_name,
                "format_version": 1,
                "version": version,
            }
        )

    def _commit_locked(
        self,
        version: int,
        body: Mapping[str, Any],
    ) -> SnapshotView:
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise DiagnosticError(
                E_SNAPSHOT_ROLLBACK,
                "snapshot version must be a positive integer",
                {"version": version},
            )
        previous = self.load()
        if previous is not None:
            if version < previous.version:
                raise DiagnosticError(
                    E_SNAPSHOT_ROLLBACK,
                    "snapshot version would roll back",
                    {
                        "actual": version,
                        "current": previous.version,
                        "path": str(self.path),
                    },
                )
            if version == previous.version:
                if dict(body) == previous.body:
                    return previous
                raise DiagnosticError(
                    E_STAGE_BUSY,
                    "snapshot version changed checksum concurrently",
                    {
                        "version": version,
                        "path": str(self.path),
                    },
                )
        body_dict = dict(body)
        checksum = self._envelope_checksum(version, body_dict)
        envelope = {
            "body": body_dict,
            "checksum": checksum,
            "format": self.format_name,
            "format_version": 1,
            "version": version,
        }
        atomic_write_json(self.path, envelope)
        return SnapshotView(version=version, body=body_dict, checksum=checksum)

    def commit(
        self,
        version: int,
        body: Mapping[str, Any],
    ) -> SnapshotView:
        with NonBlockingFileLock(self.lock_path):
            return self._commit_locked(version, body)


class LedgerManager:
    """Ledger and config snapshot are one atomically replaced bundle."""

    def __init__(
        self,
        ledger_path: str | os.PathLike[str],
        config_snapshot_path: str | os.PathLike[str] | None = None,
        *,
        lock_path: str | os.PathLike[str] | None = None,
    ):
        self.ledger_path = Path(ledger_path)
        self.config_snapshot_path = (
            Path(config_snapshot_path)
            if config_snapshot_path is not None
            else self.ledger_path.with_name("config_snapshot.json")
        )
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.ledger_path.with_name("commit.lock")
        )
        self.store = VersionedSnapshotStore(
            self.ledger_path,
            format_name="index-diag-ledger-bundle",
            lock_path=self.lock_path,
        )

    def load(self) -> dict[str, Any] | None:
        view = self.store.load()
        if view is None:
            return None
        body = view.body
        ledger = body.get("ledger")
        config_snapshot = body.get("config_snapshot")
        if not isinstance(ledger, dict) or not isinstance(config_snapshot, dict):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "ledger bundle is missing ledger or config snapshot",
                {"path": str(self.ledger_path)},
            )
        for name, document in (
            ("ledger", ledger),
            ("config_snapshot", config_snapshot),
        ):
            if document.get("checksum") != sha256_json(
                {key: value for key, value in document.items() if key != "checksum"}
            ):
                raise DiagnosticError(
                    E_SNAPSHOT_CORRUPT,
                    f"{name} checksum mismatch",
                    {"path": str(self.ledger_path)},
                )
        if ledger.get("version") != view.version:
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "ledger version differs from bundle version",
                {"path": str(self.ledger_path)},
            )
        return {
            "bundle_checksum": view.checksum,
            "bundle_version": view.version,
            "config_snapshot": config_snapshot,
            "ledger": ledger,
        }

    def commit(
        self,
        version: int,
        *,
        entries: list[dict[str, Any]],
        statistics: dict[str, Any],
        config_snapshot: dict[str, Any],
        batch_digests: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise DiagnosticError(
                E_SNAPSHOT_ROLLBACK,
                "ledger version must be a positive integer",
                {"version": version},
            )
        ledger_body = {
            "batch_digests": dict(sorted((batch_digests or {}).items())),
            "entries": entries,
            "statistics": statistics,
            "version": version,
        }
        ledger_document = seal_checksum(ledger_body)
        config_body = dict(config_snapshot)
        config_body.setdefault("version", version)
        config_document = seal_checksum(config_body)
        bundle_body = {
            "config_snapshot": config_document,
            "ledger": ledger_document,
        }
        with NonBlockingFileLock(self.lock_path):
            view = self.store._commit_locked(version, bundle_body)
            standalone = {
                "body": config_document,
                "checksum": sha256_json(config_document),
                "format": "index-diag-config-snapshot",
                "format_version": 1,
                "version": int(config_document["version"]),
            }
            atomic_write_json(self.config_snapshot_path, standalone)
        return {
            "bundle_checksum": view.checksum,
            "bundle_version": view.version,
            "config_snapshot_checksum": config_document["checksum"],
            "ledger_checksum": ledger_document["checksum"],
        }


class CodeJournal:
    """Small atomic journal used by the interactive entry point."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.lock_path = self.path.with_name(self.path.name + ".lock")

    def record(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        document = {
            "code": code,
            "details": dict(details or {}),
            "message": message,
            "operation": operation,
            "sequence": self._next_sequence(),
        }
        with NonBlockingFileLock(self.lock_path):
            atomic_write_json(self.path, document)
        return document

    def _next_sequence(self) -> int:
        try:
            previous = read_json(self.path)
        except FileNotFoundError:
            return 1
        except DiagnosticError:
            return 1
        if not isinstance(previous, dict):
            return 1
        value = previous.get("sequence", 0)
        return int(value) + 1 if isinstance(value, int) else 1

    def read(self) -> dict[str, Any] | None:
        try:
            value = read_json(self.path)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise DiagnosticError(
                E_SNAPSHOT_CORRUPT,
                "last-code journal is not an object",
                {"path": str(self.path)},
            )
        return value
