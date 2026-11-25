from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from .deterministic import canonical_json, sha256_json
from .errors import (
    BusinessError,
    E_CHECKPOINT_INVALID,
    E_SNAPSHOT_CHECKSUM,
    E_SNAPSHOT_CONFLICT,
    E_SNAPSHOT_ROLLBACK,
    E_SNAPSHOT_VERSION_AHEAD,
    E_STAGE_BUSY,
)


SNAPSHOT_KINDS = ("mask", "style", "result")


class StateDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def connect(self, *, timeout: float = 0.1) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=timeout, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self.connect(timeout=2.0)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshot_heads (
                    kind TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    kind TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    checksum TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    PRIMARY KEY (kind, version)
                );
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_key TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    stage TEXT NOT NULL,
                    region_id TEXT NOT NULL,
                    cache_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (run_key, sequence),
                    UNIQUE (run_key, stage, region_id)
                );
                CREATE TABLE IF NOT EXISTS run_log (
                    run_key TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (run_key, sequence)
                );
                """
            )
        finally:
            connection.close()


def _translate_locked(exc: sqlite3.OperationalError) -> BusinessError:
    return BusinessError(
        E_STAGE_BUSY,
        "another stage transaction is active",
        {"reason": str(exc)},
        http_status=409,
    )


class SnapshotStore:
    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    def current(self, kind: str) -> dict[str, Any] | None:
        if kind not in SNAPSHOT_KINDS:
            raise BusinessError(
                E_SNAPSHOT_CONFLICT,
                "unknown snapshot kind",
                {"kind": kind, "supported": list(SNAPSHOT_KINDS)},
            )
        connection = self.database.connect()
        try:
            head = connection.execute(
                "SELECT kind, version, checksum FROM snapshot_heads WHERE kind = ?",
                (kind,),
            ).fetchone()
            if head is None:
                return None
            row = connection.execute(
                "SELECT payload FROM snapshots WHERE kind = ? AND version = ?",
                (kind, int(head["version"])),
            ).fetchone()
            return {
                "kind": kind,
                "version": int(head["version"]),
                "checksum": str(head["checksum"]),
                "payload": json.loads(row["payload"]),
            }
        finally:
            connection.close()

    def history(self, kind: str, limit: int = 20) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT kind, version, checksum, payload
                FROM snapshots WHERE kind = ?
                ORDER BY version DESC LIMIT ?
                """,
                (kind, int(limit)),
            ).fetchall()
            return [
                {
                    "kind": row["kind"],
                    "version": int(row["version"]),
                    "checksum": row["checksum"],
                    "payload": json.loads(row["payload"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def commit(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        expected_version: int | None = None,
        expected_checksum: str | None = None,
    ) -> dict[str, Any]:
        if kind not in SNAPSHOT_KINDS:
            raise BusinessError(
                E_SNAPSHOT_CONFLICT,
                "unknown snapshot kind",
                {"kind": kind, "supported": list(SNAPSHOT_KINDS)},
            )
        checksum = sha256_json(payload)
        payload_text = canonical_json(payload)
        connection = self.database.connect(timeout=0.05)
        try:
            connection.execute("BEGIN IMMEDIATE")
            head = connection.execute(
                "SELECT version, checksum FROM snapshot_heads WHERE kind = ?",
                (kind,),
            ).fetchone()
            current_version = int(head["version"]) if head is not None else 0
            current_checksum = str(head["checksum"]) if head is not None else None
            if expected_version is not None:
                requested = int(expected_version)
                if requested < current_version:
                    connection.execute("ROLLBACK")
                    raise BusinessError(
                        E_SNAPSHOT_ROLLBACK,
                        "snapshot rollback is not permitted",
                        {
                            "kind": kind,
                            "requested_version": requested,
                            "current_version": current_version,
                        },
                        http_status=409,
                    )
                if requested > current_version:
                    connection.execute("ROLLBACK")
                    raise BusinessError(
                        E_SNAPSHOT_VERSION_AHEAD,
                        "snapshot expected version is ahead of the committed version",
                        {
                            "kind": kind,
                            "requested_version": requested,
                            "current_version": current_version,
                        },
                        http_status=409,
                    )
                if requested != current_version:
                    connection.execute("ROLLBACK")
                    raise BusinessError(
                        E_SNAPSHOT_CONFLICT,
                        "snapshot version changed",
                        {
                            "kind": kind,
                            "requested_version": requested,
                            "current_version": current_version,
                        },
                        http_status=409,
                    )
            if (
                expected_checksum is not None
                and current_checksum is not None
                and expected_checksum != current_checksum
            ):
                connection.execute("ROLLBACK")
                raise BusinessError(
                    E_SNAPSHOT_CHECKSUM,
                    "snapshot checksum changed",
                    {
                        "kind": kind,
                        "expected_checksum": expected_checksum,
                        "current_checksum": current_checksum,
                    },
                    http_status=409,
                )
            next_version = current_version + 1
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM snapshots"
            ).fetchone()
            sequence = int(sequence_row["next"])
            connection.execute(
                """
                INSERT INTO snapshots(kind, version, checksum, payload, sequence)
                VALUES (?, ?, ?, ?, ?)
                """,
                (kind, next_version, checksum, payload_text, sequence),
            )
            connection.execute(
                """
                INSERT INTO snapshot_heads(kind, version, checksum)
                VALUES (?, ?, ?)
                ON CONFLICT(kind) DO UPDATE SET
                    version = excluded.version,
                    checksum = excluded.checksum
                """,
                (kind, next_version, checksum),
            )
            connection.execute("COMMIT")
            return {
                "kind": kind,
                "version": next_version,
                "checksum": checksum,
                "replaced_version": current_version or None,
                "payload": payload,
            }
        except BusinessError:
            raise
        except sqlite3.OperationalError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise _translate_locked(exc) from exc
            raise
        finally:
            connection.close()


class CheckpointStore:
    def __init__(self, database: StateDatabase) -> None:
        self.database = database

    def save(
        self,
        run_key: str,
        *,
        stage: str,
        region_id: str,
        cache_key: str,
        content_hash: str,
    ) -> dict[str, Any]:
        connection = self.database.connect(timeout=0.5)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT sequence, cache_key, content_hash FROM checkpoints
                WHERE run_key = ? AND stage = ? AND region_id = ?
                """,
                (run_key, stage, region_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["cache_key"] != cache_key
                    or existing["content_hash"] != content_hash
                ):
                    connection.execute("ROLLBACK")
                    raise BusinessError(
                        E_CHECKPOINT_INVALID,
                        "checkpoint content no longer matches the requested stage",
                        {
                            "run_key": run_key,
                            "stage": stage,
                            "region": region_id,
                        },
                        http_status=409,
                    )
                connection.execute("COMMIT")
                return {
                    "sequence": int(existing["sequence"]),
                    "stage": stage,
                    "region": region_id,
                    "cache_key": cache_key,
                    "content_hash": content_hash,
                    "created": False,
                }
            sequence_row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next FROM checkpoints WHERE run_key = ?",
                (run_key,),
            ).fetchone()
            sequence = int(sequence_row["next"])
            connection.execute(
                """
                INSERT INTO checkpoints(
                    run_key, sequence, stage, region_id, cache_key, content_hash
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_key, sequence, stage, region_id, cache_key, content_hash),
            )
            connection.execute("COMMIT")
            return {
                "sequence": sequence,
                "stage": stage,
                "region": region_id,
                "cache_key": cache_key,
                "content_hash": content_hash,
                "created": True,
            }
        except sqlite3.OperationalError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise _translate_locked(exc) from exc
            raise
        finally:
            connection.close()

    def load(self, run_key: str) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT sequence, stage, region_id, cache_key, content_hash
                FROM checkpoints WHERE run_key = ? ORDER BY sequence
                """,
                (run_key,),
            ).fetchall()
            return [
                {
                    "sequence": int(row["sequence"]),
                    "stage": row["stage"],
                    "region": row["region_id"],
                    "cache_key": row["cache_key"],
                    "content_hash": row["content_hash"],
                }
                for row in rows
            ]
        finally:
            connection.close()

    def reset(self, run_key: str) -> int:
        connection = self.database.connect(timeout=0.5)
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM checkpoints WHERE run_key = ?", (run_key,)
            )
            connection.execute("COMMIT")
            return int(cursor.rowcount)
        except sqlite3.OperationalError as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                raise _translate_locked(exc) from exc
            raise
        finally:
            connection.close()

    def counts(self) -> list[dict[str, Any]]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT run_key, COUNT(*) AS committed
                FROM checkpoints GROUP BY run_key
                ORDER BY run_key
                """
            ).fetchall()
            return [
                {"run_key": row["run_key"], "committed": int(row["committed"])}
                for row in rows
            ]
        finally:
            connection.close()


class StageGuard:
    """Non-blocking cross-process stage lease; the second contender gets E_STAGE_BUSY."""

    def __init__(self, directory: str | Path, name: str) -> None:
        self.directory = Path(directory)
        self.name = name
        self.path = self.directory / f"{name}.lock"
        self._handle: Any | None = None

    def __enter__(self) -> "StageGuard":
        self.directory.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise BusinessError(
                E_STAGE_BUSY,
                "stage is already owned by another submitter",
                {"stage": self.name, "lock_path": str(self.path)},
                http_status=409,
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is None:
            return
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
