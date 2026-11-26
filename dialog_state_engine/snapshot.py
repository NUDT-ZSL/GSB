from __future__ import annotations

import fcntl
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, content_hash
from .errors import (
    E_FORMAT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    fail,
)
from .validation import require_int


@dataclass(frozen=True)
class Snapshot:
    version: int
    checksum: str
    payload: dict[str, Any]
    committed_at_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checksum": self.checksum,
            "payload": self.payload,
            "committed_at_ms": self.committed_at_ms,
        }


@dataclass(frozen=True)
class LoadResult:
    snapshot: Snapshot | None
    replaced_cached_snapshot: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "replaced_cached_snapshot": self.replaced_cached_snapshot,
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
        }


class SnapshotStore:
    def __init__(self, database: str | Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.database.with_suffix(self.database.suffix + ".stage.lock")
        self._cached: Snapshot | None = None
        self._initialized = False
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database,
            timeout=0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def _initialize(self) -> None:
        if self._initialized:
            return
        connection = self._connect()
        try:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshot (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    version INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    committed_at_ms INTEGER NOT NULL
                )
                """
            )
        finally:
            connection.close()
        self._initialized = True

    @contextmanager
    def _stage_file_lock(self) -> Iterator[None]:
        handle = self.lock_path.open("a+")
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                fail(
                    E_STAGE_BUSY,
                    "another snapshot stage currently holds the commit lock",
                    field="snapshot.stage_lock",
                )
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @contextmanager
    def hold_stage(self) -> Iterator[None]:
        """Hold the write transaction to exercise immediate stage-busy behavior."""
        connection = self._connect()
        try:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                fail(
                    E_STAGE_BUSY,
                    f"snapshot stage is already busy: {exc}",
                    field="snapshot.stage",
                )
            yield
        finally:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            connection.close()

    def commit(
        self,
        *,
        version: int,
        payload: dict[str, Any],
        expected_version: int | None = None,
        committed_at_ms: int | None = None,
    ) -> Snapshot:
        version = require_int(version, "version", 1)
        if not isinstance(payload, dict):
            fail(E_FORMAT_CORRUPT, "snapshot payload must be a JSON object", field="payload")
        try:
            canonical_json(payload)
        except (TypeError, ValueError) as exc:
            fail(E_FORMAT_CORRUPT, f"snapshot payload is not strict JSON: {exc}", field="payload")
        if committed_at_ms is None:
            committed_at_ms = int(time.time() * 1000)
        committed_at_ms = require_int(committed_at_ms, "committed_at_ms", 0)
        checksum = content_hash({"version": version, "payload": payload})
        payload_json = canonical_json(payload)

        with self._stage_file_lock():
            connection = self._connect()
            try:
                try:
                    connection.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                        fail(
                            E_STAGE_BUSY,
                            "another snapshot commit is in progress",
                            field="snapshot.stage",
                        )
                    fail(
                        E_FORMAT_CORRUPT,
                        f"snapshot write transaction cannot start: {exc}",
                        field="snapshot.sqlite",
                    )
                current_row = connection.execute(
                    "SELECT version, checksum FROM snapshot WHERE singleton = 1"
                ).fetchone()
                current_version = (
                    int(current_row["version"]) if current_row is not None else 0
                )
                current_checksum = (
                    str(current_row["checksum"]) if current_row is not None else None
                )
                if expected_version is not None and current_version != expected_version:
                    fail(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot base version no longer matches",
                        field="expected_version",
                        details={
                            "expected_version": expected_version,
                            "current_version": current_version,
                        },
                    )
                if version < current_version:
                    fail(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot version must increase monotonically",
                        field="version",
                        details={
                            "requested_version": version,
                            "current_version": current_version,
                        },
                    )
                if version == current_version:
                    if checksum == current_checksum:
                        connection.execute("ROLLBACK")
                        return Snapshot(
                            version=version,
                            checksum=checksum,
                            payload=payload,
                            committed_at_ms=committed_at_ms,
                        )
                    fail(
                        E_SNAPSHOT_ROLLBACK,
                        "same snapshot version cannot replace different content",
                        field="version",
                        details={"version": version},
                    )
                connection.execute("DELETE FROM snapshot")
                connection.execute(
                    """
                    INSERT INTO snapshot(
                        singleton, version, checksum, payload_json, committed_at_ms
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (version, checksum, payload_json, committed_at_ms),
                )
                connection.execute("COMMIT")
                return Snapshot(
                    version=version,
                    checksum=checksum,
                    payload=payload,
                    committed_at_ms=committed_at_ms,
                )
            except BaseException:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def load(self) -> Snapshot | None:
        return self.load_with_status().snapshot

    def load_with_status(self) -> LoadResult:
        self._initialize()
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT version, checksum, payload_json, committed_at_ms
                FROM snapshot WHERE singleton = 1
                """
            ).fetchone()
        except sqlite3.Error as exc:
            fail(
                E_FORMAT_CORRUPT,
                f"snapshot database cannot be read: {exc}",
                field="snapshot.sqlite",
            )
        finally:
            connection.close()
        if row is None:
            replaced = self._cached is not None
            self._cached = None
            return LoadResult(snapshot=None, replaced_cached_snapshot=replaced)
        try:
            payload = json.loads(row["payload_json"])
        except (TypeError, ValueError) as exc:
            fail(
                E_FORMAT_CORRUPT,
                f"stored snapshot payload is not valid JSON: {exc}",
                field="snapshot.payload",
            )
        version = require_int(row["version"], "snapshot.version", 1)
        checksum = str(row["checksum"])
        computed = content_hash({"version": version, "payload": payload})
        if checksum != computed:
            fail(
                E_FORMAT_CORRUPT,
                "stored snapshot checksum mismatch",
                field="snapshot.checksum",
                details={"expected": computed, "actual": checksum},
            )
        signature = (
            version,
            checksum,
            int(row["committed_at_ms"]),
            row["payload_json"],
        )
        previous_signature = None
        if self._cached is not None:
            previous_signature = (
                self._cached.version,
                self._cached.checksum,
                self._cached.committed_at_ms,
                canonical_json(self._cached.payload),
            )
        replaced = previous_signature is not None and previous_signature != signature
        snapshot = Snapshot(
            version=version,
            checksum=checksum,
            payload=payload,
            committed_at_ms=int(row["committed_at_ms"]),
        )
        # A version or checksum change always replaces the whole cached object.
        self._cached = snapshot
        return LoadResult(
            snapshot=snapshot,
            replaced_cached_snapshot=replaced,
        )

