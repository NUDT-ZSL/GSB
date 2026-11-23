from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import BusinessError
from .models import ScalerSpec, canonical_json
from .state import initial_state, validate_state

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore[assignment]
    import msvcrt


class FileLock:
    _thread_locks: dict[str, threading.RLock] = {}
    _thread_locks_guard = threading.Lock()

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._file: Any = None
        self._thread_lock: threading.RLock | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._thread_locks_guard:
            self._thread_lock = self._thread_locks.setdefault(
                str(self.path.resolve()), threading.RLock()
            )
        self._thread_lock.acquire()
        self._file = open(self.path, "a+b")
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)
        else:  # pragma: no cover - Windows fallback
            while True:
                try:
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    continue
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        assert self._file is not None
        if fcntl is not None:
            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        else:  # pragma: no cover - Windows fallback
            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
        self._file.close()
        self._file = None
        assert self._thread_lock is not None
        self._thread_lock.release()


class SQLiteStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.lock_path = self.db_path.with_suffix(self.db_path.suffix + ".lock")

    @contextlib.contextmanager
    def process_lock(self) -> Iterator[None]:
        with FileLock(self.lock_path):
            yield

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def initialize(self, spec: ScalerSpec) -> dict[str, Any]:
        with self.process_lock():
            connection = self._connect()
            try:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS snapshots (
                        snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        state_revision INTEGER NOT NULL,
                        round_watermark INTEGER NOT NULL,
                        state_json TEXT NOT NULL,
                        state_digest TEXT NOT NULL,
                        reason TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    );
                    CREATE TABLE IF NOT EXISTS current_snapshot (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id)
                    );
                    CREATE TABLE IF NOT EXISTS events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        snapshot_id INTEGER NOT NULL REFERENCES snapshots(snapshot_id),
                        kind TEXT NOT NULL,
                        round_no INTEGER,
                        service TEXT,
                        data_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    );
                    CREATE TABLE IF NOT EXISTS recovery_audit (
                        recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        round_no INTEGER,
                        decision_json TEXT NOT NULL,
                        created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                    );
                    CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, event_id);
                    CREATE INDEX IF NOT EXISTS idx_events_round ON events(round_no, event_id);
                    """
                )
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT snapshot_id FROM current_snapshot WHERE singleton = 1"
                ).fetchone()
                if row is not None:
                    state = self._load_with_connection(connection)
                    connection.execute("COMMIT")
                    return state
                state = initial_state(spec)
                validate_state(state)
                snapshot_id = self._insert_snapshot(
                    connection, state, reason="initialize"
                )
                connection.execute(
                    "INSERT INTO current_snapshot(singleton, snapshot_id) VALUES(1, ?)",
                    (snapshot_id,),
                )
                self._insert_events(
                    connection,
                    snapshot_id,
                    [
                        {
                            "kind": "initialized",
                            "round_no": None,
                            "service": None,
                            "data": {
                                "config_version": spec.config.config_version,
                                "spec_digest": spec.digest,
                            },
                        }
                    ],
                )
                connection.execute("COMMIT")
                return state
            except Exception:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()

    def load(self) -> tuple[dict[str, Any], int]:
        connection = self._connect()
        try:
            return self._load_unlocked(connection)
        finally:
            connection.close()

    def load_unlocked(self) -> tuple[dict[str, Any], int]:
        connection = self._connect()
        try:
            return self._load_unlocked(connection)
        finally:
            connection.close()

    def _load_unlocked(
        self, connection: sqlite3.Connection
    ) -> tuple[dict[str, Any], int]:
        row = connection.execute(
            "SELECT snapshot_id FROM current_snapshot WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise BusinessError(
                "E_STORAGE_ERROR", "database has not been initialized"
            )
        snapshot_id = int(row["snapshot_id"])
        state = self._load_with_connection(connection)
        return state, snapshot_id

    def _load_with_connection(self, connection: sqlite3.Connection) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT s.state_json
            FROM current_snapshot c
            JOIN snapshots s ON s.snapshot_id = c.snapshot_id
            WHERE c.singleton = 1
            """
        ).fetchone()
        if row is None:
            raise BusinessError("E_STORAGE_ERROR", "current snapshot pointer is missing")
        try:
            state = json.loads(row["state_json"])
        except json.JSONDecodeError as exc:
            raise BusinessError(
                "E_STORAGE_ERROR", "persisted snapshot is not valid JSON"
            ) from exc
        validate_state(state)
        return state

    def commit(
        self,
        state: dict[str, Any],
        *,
        expected_snapshot_id: int | None = None,
        reason: str,
        events: list[dict[str, Any]] | None = None,
    ) -> int:
        with self.process_lock():
            return self.commit_unlocked(
                state,
                expected_snapshot_id=expected_snapshot_id,
                reason=reason,
                events=events,
            )

    def commit_unlocked(
        self,
        state: dict[str, Any],
        *,
        expected_snapshot_id: int | None = None,
        reason: str,
        events: list[dict[str, Any]] | None = None,
    ) -> int:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT snapshot_id FROM current_snapshot WHERE singleton = 1"
            ).fetchone()
            if current is None:
                raise BusinessError(
                    "E_STORAGE_ERROR", "current snapshot pointer is missing"
                )
            current_id = int(current["snapshot_id"])
            if expected_snapshot_id is not None and current_id != expected_snapshot_id:
                raise BusinessError(
                    "E_STATE_VERSION_CONFLICT",
                    "snapshot pointer changed before commit",
                    {
                        "expected_snapshot_id": expected_snapshot_id,
                        "actual_snapshot_id": current_id,
                    },
                )
            validate_state(state)
            snapshot_id = self._insert_snapshot(connection, state, reason=reason)
            connection.execute(
                "UPDATE current_snapshot SET snapshot_id = ? WHERE singleton = 1",
                (snapshot_id,),
            )
            self._insert_events(connection, snapshot_id, events or [])
            connection.execute("COMMIT")
            return snapshot_id
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _insert_snapshot(
        self, connection: sqlite3.Connection, state: dict[str, Any], *, reason: str
    ) -> int:
        payload = canonical_json(state)
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        cursor = connection.execute(
            """
            INSERT INTO snapshots(
                state_revision, round_watermark, state_json, state_digest, reason
            ) VALUES(?, ?, ?, ?, ?)
            """,
            (
                int(state.get("state_revision", 0)),
                int(state.get("round_watermark", 0)),
                payload,
                digest,
                reason,
            ),
        )
        return int(cursor.lastrowid)

    def _insert_events(
        self,
        connection: sqlite3.Connection,
        snapshot_id: int,
        events: list[dict[str, Any]],
    ) -> None:
        for event in events:
            connection.execute(
                """
                INSERT INTO events(snapshot_id, kind, round_no, service, data_json)
                VALUES(?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    event["kind"],
                    event.get("round_no"),
                    event.get("service"),
                    canonical_json(event.get("data", {})),
                ),
            )

    def record_recovery_audit(self, round_no: int | None, decision: dict[str, Any]) -> None:
        connection = self._connect()
        try:
            connection.execute(
                "INSERT INTO recovery_audit(round_no, decision_json) VALUES(?, ?)",
                (round_no, canonical_json(decision)),
            )
        finally:
            connection.close()

    def read_events(self, limit: int = 200) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT event_id, snapshot_id, kind, round_no, service, data_json, created_at
                FROM events ORDER BY event_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            events = []
            for row in rows:
                events.append(
                    {
                        "event_id": row["event_id"],
                        "snapshot_id": row["snapshot_id"],
                        "kind": row["kind"],
                        "round_no": row["round_no"],
                        "service": row["service"],
                        "data": json.loads(row["data_json"]),
                        "created_at": row["created_at"],
                    }
                )
            return list(reversed(events))
        finally:
            connection.close()

    def read_recovery_audit(self, limit: int = 50) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT recovery_id, round_no, decision_json, created_at
                FROM recovery_audit ORDER BY recovery_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "recovery_id": row["recovery_id"],
                    "round_no": row["round_no"],
                    "decision": json.loads(row["decision_json"]),
                    "created_at": row["created_at"],
                }
                for row in reversed(rows)
            ]
        finally:
            connection.close()

    def snapshot_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT snapshot_id, state_revision, round_watermark, state_digest,
                       reason, created_at
                FROM snapshots ORDER BY snapshot_id DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [
                {
                    "snapshot_id": row["snapshot_id"],
                    "state_revision": row["state_revision"],
                    "round_watermark": row["round_watermark"],
                    "state_digest": row["state_digest"],
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                }
                for row in reversed(rows)
            ]
        finally:
            connection.close()
