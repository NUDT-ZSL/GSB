from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import EngineError, fail
from .files import fsync_directory
from .locking import lock_is_held, partition_lock_path


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StateStore:
    def __init__(self, state_dir: str | Path, *, read_only: bool = False) -> None:
        self.root = Path(state_dir).resolve()
        self.db_path = self.root / "state.sqlite3"
        self.lock_root = self.root / "locks"
        self.read_only = read_only
        if read_only:
            if not self.db_path.exists():
                fail(
                    "E_NOT_FOUND",
                    "state database does not exist",
                    details={"state_dir": str(self.root)},
                    exit_code=3,
                )
        else:
            self.root.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def _connect(self) -> sqlite3.Connection:
        if self.read_only:
            connection = sqlite3.connect(
                f"file:{self.db_path}?mode=ro",
                uri=True,
                timeout=30.0,
                isolation_level=None,
            )
        else:
            connection = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA foreign_keys=ON")
        if self.read_only:
            connection.execute("PRAGMA query_only=ON")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS engine_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS queries (
                    query_id TEXT PRIMARY KEY,
                    current_version INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_algorithm TEXT,
                    result_path TEXT,
                    result_digest TEXT,
                    result_rows INTEGER,
                    logical_clock REAL NOT NULL DEFAULT 0,
                    metrics_json TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS versions (
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    selected_algorithm TEXT,
                    decision_json TEXT,
                    result_path TEXT,
                    result_digest TEXT,
                    result_rows INTEGER,
                    metrics_json TEXT,
                    logical_clock REAL NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (query_id, version)
                );
                CREATE TABLE IF NOT EXISTS data_stats (
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    digest TEXT NOT NULL,
                    stats_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (query_id, version, side)
                );
                CREATE TABLE IF NOT EXISTS algorithm_decisions (
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    algorithm TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    logical_clock REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (query_id, version)
                );
                CREATE TABLE IF NOT EXISTS partition_commits (
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    partition_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    rows INTEGER NOT NULL,
                    bytes INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    key_digest TEXT NOT NULL,
                    input_digest TEXT NOT NULL,
                    clock_cost REAL NOT NULL,
                    logical_clock REAL NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (query_id, version, side, partition_id)
                );
                CREATE TABLE IF NOT EXISTS result_partition_commits (
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    requested_algorithm TEXT NOT NULL,
                    actual_algorithm TEXT NOT NULL,
                    partition_id INTEGER NOT NULL,
                    relative_path TEXT NOT NULL,
                    rows INTEGER NOT NULL,
                    bytes INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    clock_cost REAL NOT NULL,
                    logical_clock REAL NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (query_id, version, partition_id)
                );
                CREATE TABLE IF NOT EXISTS result_ledger (
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    result_path TEXT NOT NULL,
                    result_digest TEXT NOT NULL,
                    result_rows INTEGER NOT NULL,
                    result_bytes INTEGER NOT NULL,
                    logical_clock REAL NOT NULL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (query_id, version)
                );
                CREATE TABLE IF NOT EXISTS query_rounds (
                    round_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    pid INTEGER NOT NULL,
                    start_clock REAL NOT NULL,
                    finish_clock REAL,
                    status TEXT NOT NULL,
                    details_json TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS benchmark_runs (
                    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_digest TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_partition_commits_version
                    ON partition_commits(query_id, version, side, partition_id);
                CREATE INDEX IF NOT EXISTS idx_result_partitions_version
                    ON result_partition_commits(query_id, version, partition_id);
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO engine_meta(key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )
            connection.execute(
                "INSERT OR REPLACE INTO engine_meta(key, value) VALUES (?, ?)",
                ("created_by", "local-shuffle-join-engine"),
            )
        finally:
            connection.close()
        fsync_directory(self.root)

    def path_for(self, relative_path: str) -> Path:
        path = Path(relative_path)
        if path.is_absolute():
            return path
        return self.root / path

    def relative(self, path: Path) -> str:
        try:
            return str(path.resolve().relative_to(self.root))
        except ValueError:
            return str(path.resolve())

    def _assert_current_version(self, connection: sqlite3.Connection, query_id: str, version: int) -> None:
        row = connection.execute(
            "SELECT current_version FROM queries WHERE query_id = ?",
            (query_id,),
        ).fetchone()
        if row is None:
            fail("E_NOT_FOUND", f"query is not initialized: {query_id}")
        if int(row["current_version"]) != int(version):
            fail(
                "E_VERSION_CONFLICT",
                "query version changed while the operation was in progress",
                details={
                    "query_id": query_id,
                    "expected_version": version,
                    "current_version": int(row["current_version"]),
                },
                exit_code=3,
            )

    def ensure_version(
        self,
        query_id: str,
        fingerprint: str,
        *,
        input_digest: str | None = None,
    ) -> dict[str, Any]:
        now = utc_now()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM queries WHERE query_id = ?", (query_id,)
            ).fetchone()
            if row is not None and row["fingerprint"] == fingerprint:
                return {
                    "version": int(row["current_version"]),
                    "created": False,
                    "status": row["status"],
                    "fingerprint": fingerprint,
                }
            if row is None:
                version = 1
                connection.execute(
                    """
                    INSERT INTO queries(
                        query_id, current_version, fingerprint, status,
                        selected_algorithm, result_path, result_digest, result_rows,
                        logical_clock, metrics_json, updated_at
                    ) VALUES (?, ?, ?, 'planned', NULL, NULL, NULL, NULL, 0, NULL, ?)
                    """,
                    (query_id, version, fingerprint, now),
                )
            else:
                maximum = connection.execute(
                    "SELECT COALESCE(MAX(version), 0) AS maximum FROM versions WHERE query_id = ?",
                    (query_id,),
                ).fetchone()["maximum"]
                version = int(maximum) + 1
                connection.execute(
                    """
                    UPDATE queries
                       SET current_version = ?, fingerprint = ?, status = 'planned',
                           selected_algorithm = NULL, result_path = NULL,
                           result_digest = NULL, result_rows = NULL,
                           logical_clock = 0, metrics_json = NULL, updated_at = ?
                     WHERE query_id = ?
                    """,
                    (version, fingerprint, now, query_id),
                )
            connection.execute(
                """
                INSERT INTO versions(
                    query_id, version, fingerprint, status, selected_algorithm,
                    decision_json, result_path, result_digest, result_rows,
                    metrics_json, logical_clock, created_at, updated_at
                ) VALUES (?, ?, ?, 'planned', NULL, NULL, NULL, NULL, NULL, NULL, 0, ?, ?)
                """,
                (query_id, version, fingerprint, now, now),
            )
            return {
                "version": version,
                "created": True,
                "status": "planned",
                "fingerprint": fingerprint,
                "input_digest": input_digest,
            }

    def get_current(self, query_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM queries WHERE query_id = ?", (query_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def get_version(self, query_id: str, version: int) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM versions WHERE query_id = ? AND version = ?",
                (query_id, version),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def record_stats(
        self,
        query_id: str,
        version: int,
        side: str,
        digest: str,
        stats: dict[str, Any],
        logical_clock: float,
    ) -> None:
        now = utc_now()
        payload = json.dumps(stats, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            connection.execute(
                """
                INSERT INTO data_stats(query_id, version, side, digest, stats_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_id, version, side) DO UPDATE SET
                    digest = excluded.digest,
                    stats_json = excluded.stats_json,
                    created_at = excluded.created_at
                """,
                (query_id, version, side, digest, payload, now),
            )
            connection.execute(
                """
                UPDATE versions
                   SET status = CASE WHEN status = 'planned' THEN 'observed' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND version = ?
                """,
                (logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE queries
                   SET logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND current_version = ?
                """,
                (logical_clock, now, query_id, version),
            )

    def record_decision(
        self,
        query_id: str,
        version: int,
        algorithm: str,
        decision: dict[str, Any],
        logical_clock: float,
    ) -> None:
        now = utc_now()
        payload = json.dumps(decision, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            existing = connection.execute(
                """
                SELECT algorithm, decision_json FROM algorithm_decisions
                 WHERE query_id = ? AND version = ?
                """,
                (query_id, version),
            ).fetchone()
            if existing is not None:
                if existing["algorithm"] != algorithm or existing["decision_json"] != payload:
                    fail(
                        "E_COMMIT_CONFLICT",
                        "algorithm decision already exists with different content",
                        details={"query_id": query_id, "version": version},
                        exit_code=3,
                    )
            else:
                connection.execute(
                    """
                    INSERT INTO algorithm_decisions(
                        query_id, version, algorithm, decision_json, logical_clock, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (query_id, version, algorithm, payload, logical_clock, now),
                )
            connection.execute(
                """
                UPDATE versions
                   SET selected_algorithm = ?, decision_json = ?,
                       status = CASE WHEN status IN ('planned', 'observed') THEN 'planned' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND version = ?
                """,
                (algorithm, payload, logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE queries
                   SET selected_algorithm = ?, status = CASE
                         WHEN status IN ('planned', 'observed') THEN 'planned' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND current_version = ?
                """,
                (algorithm, logical_clock, now, query_id, version),
            )

    def set_status(self, query_id: str, version: int, status: str, logical_clock: float) -> None:
        now = utc_now()
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            connection.execute(
                """
                UPDATE versions SET status = ?, logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND version = ?
                """,
                (status, logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE queries SET status = ?, logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND current_version = ?
                """,
                (status, logical_clock, now, query_id, version),
            )

    def start_round(self, query_id: str, version: int, action: str, start_clock: float) -> int:
        now = utc_now()
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            stale = connection.execute(
                """
                SELECT round_id, pid FROM query_rounds
                 WHERE query_id = ? AND version = ? AND status = 'running'
                """,
                (query_id, version),
            ).fetchall()
            for row in stale:
                if int(row["pid"]) == os.getpid() or _pid_alive(int(row["pid"])):
                    continue
                connection.execute(
                    """
                    UPDATE query_rounds
                       SET status = 'interrupted', finish_clock = ?, finished_at = ?,
                           details_json = ?
                     WHERE round_id = ?
                    """,
                    (
                        start_clock,
                        now,
                        json.dumps(
                            {"reason": "process no longer alive"},
                            sort_keys=True,
                        ),
                        int(row["round_id"]),
                    ),
                )
            cursor = connection.execute(
                """
                INSERT INTO query_rounds(
                    query_id, version, action, pid, start_clock, finish_clock,
                    status, details_json, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, NULL, 'running', NULL, ?, NULL)
                """,
                (query_id, version, action, os.getpid(), start_clock, now),
            )
            return int(cursor.lastrowid)

    def finish_round(
        self,
        round_id: int,
        *,
        status: str,
        finish_clock: float,
        details: dict[str, Any] | None = None,
    ) -> None:
        payload = (
            json.dumps(details, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
            if details is not None
            else None
        )
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE query_rounds
                   SET status = ?, finish_clock = ?, details_json = ?, finished_at = ?
                 WHERE round_id = ?
                """,
                (status, finish_clock, payload, utc_now(), round_id),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def get_partition_commits(
        self, query_id: str, version: int, side: str | None = None
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if side is None:
                cursor = connection.execute(
                    """
                    SELECT * FROM partition_commits
                     WHERE query_id = ? AND version = ?
                     ORDER BY side, partition_id
                    """,
                    (query_id, version),
                )
            else:
                cursor = connection.execute(
                    """
                    SELECT * FROM partition_commits
                     WHERE query_id = ? AND version = ? AND side = ?
                     ORDER BY partition_id
                    """,
                    (query_id, version, side),
                )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def get_partition_commit(
        self, query_id: str, version: int, side: str, partition_id: int
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM partition_commits
                 WHERE query_id = ? AND version = ? AND side = ? AND partition_id = ?
                """,
                (query_id, version, side, partition_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def commit_partition(
        self,
        query_id: str,
        version: int,
        side: str,
        partition_id: int,
        *,
        relative_path: str,
        rows: int,
        size_bytes: int,
        digest: str,
        key_digest: str,
        input_digest: str,
        clock_cost: float,
        logical_clock: float,
    ) -> bool:
        now = utc_now()
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            existing = connection.execute(
                """
                SELECT * FROM partition_commits
                 WHERE query_id = ? AND version = ? AND side = ? AND partition_id = ?
                """,
                (query_id, version, side, partition_id),
            ).fetchone()
            if existing is not None:
                expected = (digest, rows, input_digest, key_digest)
                actual = (
                    existing["digest"],
                    int(existing["rows"]),
                    existing["input_digest"],
                    existing["key_digest"],
                )
                if expected != actual:
                    fail(
                        "E_COMMIT_CONFLICT",
                        "partition was already committed with different content",
                        details={
                            "side": side,
                            "partition_id": partition_id,
                            "existing_digest": existing["digest"],
                            "new_digest": digest,
                        },
                        exit_code=3,
                    )
                return False
            connection.execute(
                """
                INSERT INTO partition_commits(
                    query_id, version, side, partition_id, relative_path,
                    rows, bytes, digest, key_digest, input_digest,
                    clock_cost, logical_clock, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    version,
                    side,
                    partition_id,
                    relative_path,
                    rows,
                    size_bytes,
                    digest,
                    key_digest,
                    input_digest,
                    clock_cost,
                    logical_clock,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE versions
                   SET status = CASE WHEN status IN ('planned', 'observed') THEN 'shuffling' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND version = ?
                """,
                (logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE queries
                   SET status = CASE WHEN status IN ('planned', 'observed') THEN 'shuffling' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND current_version = ?
                """,
                (logical_clock, now, query_id, version),
            )
            return True

    def get_result_commits(self, query_id: str, version: int) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            cursor = connection.execute(
                """
                SELECT * FROM result_partition_commits
                 WHERE query_id = ? AND version = ?
                 ORDER BY partition_id
                """,
                (query_id, version),
            )
            return [dict(row) for row in cursor.fetchall()]
        finally:
            connection.close()

    def get_result_commit(
        self, query_id: str, version: int, partition_id: int
    ) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM result_partition_commits
                 WHERE query_id = ? AND version = ? AND partition_id = ?
                """,
                (query_id, version, partition_id),
            ).fetchone()
            return dict(row) if row else None
        finally:
            connection.close()

    def commit_result_partition(
        self,
        query_id: str,
        version: int,
        partition_id: int,
        *,
        requested_algorithm: str,
        actual_algorithm: str,
        relative_path: str,
        rows: int,
        size_bytes: int,
        digest: str,
        clock_cost: float,
        logical_clock: float,
    ) -> bool:
        now = utc_now()
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            existing = connection.execute(
                """
                SELECT * FROM result_partition_commits
                 WHERE query_id = ? AND version = ? AND partition_id = ?
                """,
                (query_id, version, partition_id),
            ).fetchone()
            if existing is not None:
                expected = (digest, rows, requested_algorithm)
                actual = (
                    existing["digest"],
                    int(existing["rows"]),
                    existing["requested_algorithm"],
                )
                if expected != actual:
                    fail(
                        "E_COMMIT_CONFLICT",
                        "result partition was already committed with different content",
                        details={
                            "partition_id": partition_id,
                            "existing_digest": existing["digest"],
                            "new_digest": digest,
                        },
                        exit_code=3,
                    )
                return False
            connection.execute(
                """
                INSERT INTO result_partition_commits(
                    query_id, version, requested_algorithm, actual_algorithm,
                    partition_id, relative_path, rows, bytes, digest,
                    clock_cost, logical_clock, committed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    version,
                    requested_algorithm,
                    actual_algorithm,
                    partition_id,
                    relative_path,
                    rows,
                    size_bytes,
                    digest,
                    clock_cost,
                    logical_clock,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE versions
                   SET status = CASE WHEN status IN ('planned', 'observed', 'shuffling')
                                     THEN 'joining' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND version = ?
                """,
                (logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE queries
                   SET status = CASE WHEN status IN ('planned', 'observed', 'shuffling')
                                     THEN 'joining' ELSE status END,
                       logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND current_version = ?
                """,
                (logical_clock, now, query_id, version),
            )
            return True

    def finalize_result(
        self,
        query_id: str,
        version: int,
        *,
        result_path: str,
        result_digest: str,
        result_rows: int,
        result_bytes: int,
        logical_clock: float,
        metrics: dict[str, Any],
    ) -> bool:
        now = utc_now()
        metrics_json = json.dumps(
            metrics, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            existing = connection.execute(
                """
                SELECT * FROM result_ledger WHERE query_id = ? AND version = ?
                """,
                (query_id, version),
            ).fetchone()
            if existing is not None:
                if existing["result_digest"] == result_digest:
                    return False
                fail(
                    "E_COMMIT_CONFLICT",
                    "result ledger already exists with a different digest",
                    details={
                        "existing_digest": existing["result_digest"],
                        "new_digest": result_digest,
                    },
                    exit_code=3,
                )
            connection.execute(
                """
                INSERT INTO result_ledger(
                    query_id, version, result_path, result_digest, result_rows,
                    result_bytes, logical_clock, metrics_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    query_id,
                    version,
                    result_path,
                    result_digest,
                    result_rows,
                    result_bytes,
                    logical_clock,
                    metrics_json,
                    now,
                ),
            )
            connection.execute(
                """
                UPDATE versions
                   SET status = 'completed', result_path = ?, result_digest = ?,
                       result_rows = ?, metrics_json = ?, logical_clock = MAX(logical_clock, ?),
                       updated_at = ?
                 WHERE query_id = ? AND version = ?
                """,
                (
                    result_path,
                    result_digest,
                    result_rows,
                    metrics_json,
                    logical_clock,
                    now,
                    query_id,
                    version,
                ),
            )
            connection.execute(
                """
                UPDATE queries
                   SET status = 'completed', result_path = ?, result_digest = ?,
                       result_rows = ?, metrics_json = ?, logical_clock = MAX(logical_clock, ?),
                       updated_at = ?
                 WHERE query_id = ? AND current_version = ?
                """,
                (
                    result_path,
                    result_digest,
                    result_rows,
                    metrics_json,
                    logical_clock,
                    now,
                    query_id,
                    version,
                ),
            )
            return True

    def mark_failed(
        self,
        query_id: str,
        version: int,
        message: str,
        *,
        logical_clock: float,
    ) -> None:
        now = utc_now()
        with self.transaction() as connection:
            self._assert_current_version(connection, query_id, version)
            connection.execute(
                """
                UPDATE versions SET status = 'failed', logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND version = ? AND status != 'completed'
                """,
                (logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE queries SET status = 'failed', logical_clock = MAX(logical_clock, ?), updated_at = ?
                 WHERE query_id = ? AND current_version = ? AND status != 'completed'
                """,
                (logical_clock, now, query_id, version),
            )
            connection.execute(
                """
                UPDATE query_rounds SET details_json = ?
                 WHERE round_id = (
                    SELECT round_id FROM query_rounds
                     WHERE query_id = ? AND version = ? AND status = 'running'
                     ORDER BY round_id DESC LIMIT 1
                 )
                """,
                (json.dumps({"failure": message}, sort_keys=True), query_id, version),
            )

    def get_stats(self, query_id: str, version: int) -> dict[str, dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT side, digest, stats_json FROM data_stats
                 WHERE query_id = ? AND version = ? ORDER BY side
                """,
                (query_id, version),
            ).fetchall()
            return {
                row["side"]: {
                    "digest": row["digest"],
                    "stats": json.loads(row["stats_json"]),
                }
                for row in rows
            }
        finally:
            connection.close()

    def get_decision(self, query_id: str, version: int) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM algorithm_decisions WHERE query_id = ? AND version = ?
                """,
                (query_id, version),
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            payload["decision"] = json.loads(payload.pop("decision_json"))
            return payload
        finally:
            connection.close()

    def get_ledger(self, query_id: str, version: int) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM result_ledger WHERE query_id = ? AND version = ?",
                (query_id, version),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["metrics"] = json.loads(result.pop("metrics_json"))
            return result
        finally:
            connection.close()

    def get_rounds(self, query_id: str, version: int, limit: int = 20) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM query_rounds
                 WHERE query_id = ? AND version = ?
                 ORDER BY round_id DESC LIMIT ?
                """,
                (query_id, version, limit),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                raw_details = item.pop("details_json", None)
                item["details"] = json.loads(raw_details) if raw_details else None
                results.append(item)
            return results
        finally:
            connection.close()

    def partition_temp_files(self, version: int, side: str) -> list[str]:
        directory = self.root / "data" / f"v{version}" / "shuffle" / side
        if not directory.exists():
            return []
        return sorted(path.name for path in directory.glob(".tmp-*"))

    def result_temp_files(self, version: int) -> list[str]:
        directory = self.root / "data" / f"v{version}" / "results"
        if not directory.exists():
            return []
        return sorted(str(path.relative_to(self.root)) for path in directory.rglob(".tmp-*"))

    def partition_lock_path(
        self,
        query_id: str,
        version: int,
        kind: str,
        partition_id: int,
        *,
        side: str | None = None,
    ) -> Path:
        return partition_lock_path(
            self.lock_root,
            query_id,
            version,
            kind,
            partition_id,
            side=side,
        )

    def build_status(self, query_id: str) -> dict[str, Any]:
        current = self.get_current(query_id)
        if current is None:
            return {
                "ok": True,
                "query_id": query_id,
                "state": "absent",
                "partitions": {"left": [], "right": []},
                "result_partitions": [],
            }
        version = int(current["current_version"])
        stats = self.get_stats(query_id, version)
        decision = self.get_decision(query_id, version)
        ledger = self.get_ledger(query_id, version)
        commits = {
            "left": {
                int(row["partition_id"]): row
                for row in self.get_partition_commits(query_id, version, "left")
            },
            "right": {
                int(row["partition_id"]): row
                for row in self.get_partition_commits(query_id, version, "right")
            },
        }
        result_commits = {
            int(row["partition_id"]): row for row in self.get_result_commits(query_id, version)
        }
        declared_partitions = 0
        if stats:
            declared_partitions = len(stats["left"]["stats"]["partition_row_counts"])
        if declared_partitions == 0:
            max_committed = max(
                [*commits["left"].keys(), *commits["right"].keys(), *result_commits.keys()],
                default=-1,
            )
            declared_partitions = max_committed + 1
        partitions: dict[str, list[dict[str, Any]]] = {"left": [], "right": []}
        for side in ("left", "right"):
            temp_names = self.partition_temp_files(version, side)
            for partition_id in range(declared_partitions):
                commit = commits[side].get(partition_id)
                lock_path = self.partition_lock_path(
                    query_id, version, "partition", partition_id, side=side
                )
                status = "missing"
                if commit is not None:
                    status = "committed"
                elif lock_is_held(lock_path):
                    status = "in_progress"
                elif any(
                    f"part-{partition_id:05d}" in name for name in temp_names
                ):
                    status = "partial"
                partitions[side].append(
                    {
                        "partition": partition_id,
                        "status": status,
                        "rows": int(commit["rows"]) if commit else None,
                        "bytes": int(commit["bytes"]) if commit else None,
                        "digest": commit["digest"] if commit else None,
                        "key_digest": commit["key_digest"] if commit else None,
                        "path": commit["relative_path"] if commit else None,
                        "logical_clock": float(commit["logical_clock"]) if commit else None,
                    }
                )
        result_partitions = []
        result_temp_names = self.result_temp_files(version)
        for partition_id in range(declared_partitions):
            commit = result_commits.get(partition_id)
            lock_path = self.partition_lock_path(
                query_id, version, "result", partition_id
            )
            status = "missing"
            if commit is not None:
                status = "committed"
            elif lock_is_held(lock_path):
                status = "in_progress"
            elif any(
                f"part-{partition_id:05d}" in name for name in result_temp_names
            ):
                status = "partial"
            result_partitions.append(
                {
                    "partition": partition_id,
                    "status": status,
                    "rows": int(commit["rows"]) if commit else None,
                    "bytes": int(commit["bytes"]) if commit else None,
                    "digest": commit["digest"] if commit else None,
                    "path": commit["relative_path"] if commit else None,
                    "requested_algorithm": commit["requested_algorithm"] if commit else None,
                    "actual_algorithm": commit["actual_algorithm"] if commit else None,
                    "logical_clock": float(commit["logical_clock"]) if commit else None,
                }
            )
        current_metrics = json.loads(current["metrics_json"]) if current.get("metrics_json") else None
        return {
            "ok": True,
            "query_id": query_id,
            "state": "present",
            "current_version": version,
            "fingerprint": current["fingerprint"],
            "status": current["status"],
            "selected_algorithm": current["selected_algorithm"],
            "algorithm_decision": decision["decision"] if decision else None,
            "logical_clock_watermark": float(current["logical_clock"]),
            "result_pointer": {
                "path": current["result_path"],
                "digest": current["result_digest"],
                "rows": current["result_rows"],
            },
            "metrics": current_metrics,
            "tables": {
                side: (
                    {
                        "digest": value["digest"],
                        "rows": value["stats"]["rows"],
                        "generated_rows_digest": value["stats"]["generated_rows_digest"],
                        "key_distribution_digest": value["stats"]["key_distribution_digest"],
                        "null_key_count": value["stats"]["null_key_count"],
                        "distinct_non_null_keys": value["stats"]["distinct_non_null_keys"],
                        "top_keys": value["stats"]["top_keys"],
                        "partition_row_counts": value["stats"]["partition_row_counts"],
                    }
                    if value
                    else None
                )
                for side, value in stats.items()
            },
            "partitions": partitions,
            "result_partitions": result_partitions,
            "result_ledger": (
                {
                    "path": ledger["result_path"],
                    "digest": ledger["result_digest"],
                    "rows": int(ledger["result_rows"]),
                    "bytes": int(ledger["result_bytes"]),
                    "logical_clock": float(ledger["logical_clock"]),
                }
                if ledger
                else None
            ),
            "rounds": self.get_rounds(query_id, version),
            "partial_files": {
                "shuffle": {
                    "left": self.partition_temp_files(version, "left"),
                    "right": self.partition_temp_files(version, "right"),
                },
                "result": self.result_temp_files(version),
            },
        }

    def record_benchmark(self, report: dict[str, Any], digest: str) -> None:
        payload = json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO benchmark_runs(report_digest, report_json, created_at)
                VALUES (?, ?, ?)
                """,
                (digest, payload, utc_now()),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()


def open_existing_store(state_dir: str | Path) -> StateStore:
    path = Path(state_dir)
    if not (path / "state.sqlite3").exists():
        fail(
            "E_NOT_FOUND",
            "state database does not exist",
            details={"state_dir": str(path.resolve())},
            exit_code=3,
        )
    return StateStore(path, read_only=True)


def safe_json_load(payload: str) -> Any:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise EngineError(
            "E_STATE_CORRUPT",
            "state database contains invalid JSON",
            {"error": exc.msg},
            3,
        ) from exc
