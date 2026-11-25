from __future__ import annotations

import fcntl
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .errors import ConflictError
from .utils import canonical_json, digest_value, utc_wall_now


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS input_bundles (
    name TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    loaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    round_id TEXT NOT NULL,
    action TEXT NOT NULL,
    parent_version INTEGER NOT NULL,
    digest TEXT NOT NULL,
    chain_digest TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    created_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    request_key TEXT PRIMARY KEY,
    op_id TEXT NOT NULL,
    round_id TEXT NOT NULL,
    action TEXT NOT NULL,
    job_id TEXT,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    request_json TEXT NOT NULL,
    result_json TEXT,
    error_code TEXT,
    ledger_version INTEGER,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_wall TEXT NOT NULL,
    updated_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotent_results (
    request_key TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    action TEXT NOT NULL,
    job_id TEXT,
    result_json TEXT NOT NULL,
    ledger_version INTEGER NOT NULL,
    created_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS occupancy_intervals (
    ledger_version INTEGER NOT NULL,
    card_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    start_share INTEGER NOT NULL,
    end_share INTEGER NOT NULL,
    lease_id TEXT NOT NULL,
    job_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    PRIMARY KEY (ledger_version, card_id, resource, start_share, lease_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ledger_version INTEGER,
    round_id TEXT,
    clock_ms INTEGER,
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    fields_json TEXT NOT NULL,
    wall_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS injection_runs (
    injection_id TEXT PRIMARY KEY,
    phase TEXT NOT NULL,
    fault_type TEXT NOT NULL,
    round_id TEXT,
    job_id TEXT,
    card_id TEXT,
    status TEXT NOT NULL,
    error_code TEXT NOT NULL,
    result_json TEXT NOT NULL,
    run_count INTEGER NOT NULL DEFAULT 1,
    updated_wall TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL,
    lease_id TEXT NOT NULL,
    generation INTEGER NOT NULL,
    quota_group TEXT NOT NULL,
    compute_shares INTEGER NOT NULL,
    memory_shares INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_clock INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    updated_wall TEXT NOT NULL
);
"""


class FileLockTimeout(RuntimeError):
    pass


class SchedulerStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(str(self.db_path) + ".lock")

    @contextmanager
    def file_lock(self, timeout_seconds: float = 60.0) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+")
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise FileLockTimeout(
                            f"timed out waiting for scheduler file lock: {self.lock_path}"
                        )
                    time.sleep(0.05)
            yield
        finally:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            self.init_schema(conn)
            yield conn
        finally:
            conn.close()

    def init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        row = conn.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('ledger_version', '0')"
            )

    @staticmethod
    def begin_immediate(conn: sqlite3.Connection) -> None:
        conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def current_version(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()
        return int(row["value"]) if row else 0

    @staticmethod
    def set_pointer(conn: sqlite3.Connection, version: int) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('ledger_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(version),),
        )

    @staticmethod
    def compare_and_swap_pointer(
        conn: sqlite3.Connection, expected_version: int, new_version: int
    ) -> None:
        cursor = conn.execute(
            """
            UPDATE meta
               SET value=?
             WHERE key='ledger_version' AND value=?
            """,
            (str(new_version), str(expected_version)),
        )
        if cursor.rowcount != 1:
            actual = SchedulerStore.current_version(conn)
            raise ConflictError(
                "ledger pointer changed before commit",
                details={
                    "expected_version": expected_version,
                    "new_version": new_version,
                    "actual_version": actual,
                },
                error_code="E_LEDGER_VERSION_CONFLICT",
            )

    @staticmethod
    def load_snapshot(conn: sqlite3.Connection, version: Optional[int] = None) -> Dict[str, Any]:
        if version is None:
            version = SchedulerStore.current_version(conn)
        row = conn.execute(
            "SELECT snapshot_json FROM ledger_versions WHERE version=?",
            (version,),
        ).fetchone()
        if row is None:
            raise KeyError(f"ledger version {version} does not exist")
        import json

        return json.loads(row["snapshot_json"])

    @staticmethod
    def parent_chain_digest(conn: sqlite3.Connection, version: int) -> str:
        if version == 0:
            return "0" * 64
        row = conn.execute(
            "SELECT chain_digest FROM ledger_versions WHERE version=?",
            (version,),
        ).fetchone()
        return row["chain_digest"] if row else "0" * 64

    def insert_intent(
        self,
        conn: sqlite3.Connection,
        *,
        request_key: str,
        op_id: str,
        round_id: str,
        action: str,
        job_id: Optional[str],
        request_hash: str,
        request_json: str,
    ) -> None:
        now = utc_wall_now()
        conn.execute(
            """
            INSERT INTO operations(
                request_key, op_id, round_id, action, job_id, status, stage,
                request_hash, request_json, attempts, created_wall, updated_wall
            ) VALUES(?, ?, ?, ?, ?, 'planned', 'intent', ?, ?, 0, ?, ?)
            ON CONFLICT(request_key) DO NOTHING
            """,
            (
                request_key,
                op_id,
                round_id,
                action,
                job_id,
                request_hash,
                request_json,
                now,
                now,
            ),
        )

    def get_operation(self, conn: sqlite3.Connection, request_key: str) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM operations WHERE request_key=?", (request_key,)
        ).fetchone()

    def get_idempotent_result(
        self, conn: sqlite3.Connection, request_key: str
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM idempotent_results WHERE request_key=?", (request_key,)
        ).fetchone()

    def commit_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        base_version: int,
        round_id: str,
        action: str,
        snapshot: Dict[str, Any],
        request_key: str,
        result: Dict[str, Any],
        event: Optional[Dict[str, Any]] = None,
    ) -> int:
        """Insert a complete new ledger version and atomically swap pointer."""

        version = base_version + 1
        snapshot["pointer_version"] = version
        snapshot_digest = digest_value(snapshot)
        parent_chain = self.parent_chain_digest(conn, base_version)
        chain_digest = digest_value(
            {"parent_chain_digest": parent_chain, "snapshot_digest": snapshot_digest, "version": version}
        )
        now = utc_wall_now()
        conn.execute(
            """
            INSERT INTO ledger_versions(
                version, round_id, action, parent_version, digest, chain_digest,
                snapshot_json, created_wall
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version,
                round_id,
                action,
                base_version,
                snapshot_digest,
                chain_digest,
                canonical_json(snapshot),
                now,
            ),
        )
        self.compare_and_swap_pointer(conn, base_version, version)
        self.replace_occupancy(conn, version, snapshot)
        conn.execute(
            """
            INSERT INTO idempotent_results(
                request_key, round_id, action, job_id, result_json,
                ledger_version, created_wall
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(request_key) DO UPDATE SET
                result_json=excluded.result_json,
                ledger_version=excluded.ledger_version
            """,
            (
                request_key,
                round_id,
                action,
                result.get("job_id"),
                canonical_json(result),
                version,
                now,
            ),
        )
        conn.execute(
            """
            UPDATE operations
               SET status='applied', stage='ledger_committed', result_json=?,
                   error_code=NULL, ledger_version=?, attempts=attempts+1,
                   updated_wall=?
             WHERE request_key=?
            """,
            (canonical_json(result), version, now, request_key),
        )
        if event is not None:
            self.add_event(
                conn,
                ledger_version=version,
                round_id=round_id,
                clock_ms=snapshot.get("clock_ms"),
                level=event.get("level", "info"),
                code=event.get("code", action),
                message=event.get("message", ""),
                fields=event.get("fields", {}),
            )
        return version

    @staticmethod
    def replace_occupancy(
        conn: sqlite3.Connection, version: int, snapshot: Dict[str, Any]
    ) -> None:
        conn.execute("DELETE FROM occupancy_intervals WHERE ledger_version=?", (version,))
        for lease in snapshot.get("leases", {}).values():
            if lease.get("status") != "active":
                continue
            for resource, interval_field in (
                ("compute", "compute_interval"),
                ("memory", "memory_interval"),
            ):
                start, end = lease[interval_field]
                conn.execute(
                    """
                    INSERT INTO occupancy_intervals(
                        ledger_version, card_id, resource, start_share, end_share,
                        lease_id, job_id, generation
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version,
                        lease["card_id"],
                        resource,
                        int(start),
                        int(end),
                        lease["lease_id"],
                        lease["job_id"],
                        int(lease["generation"]),
                    ),
                )

    @staticmethod
    def add_event(
        conn: sqlite3.Connection,
        *,
        ledger_version: Optional[int],
        round_id: Optional[str],
        clock_ms: Optional[int],
        level: str = "info",
        code: str,
        message: str,
        fields: Optional[Dict[str, Any]] = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_events(
                ledger_version, round_id, clock_ms, level, code, message,
                fields_json, wall_ts
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ledger_version,
                round_id,
                clock_ms,
                level,
                code,
                message,
                canonical_json(fields or {}),
                utc_wall_now(),
            ),
        )

    def record_injection_run(
        self,
        *,
        injection_id: str,
        phase: str,
        fault_type: str,
        round_id: Optional[str],
        job_id: Optional[str],
        card_id: Optional[str],
        error_code: str,
        result: Dict[str, Any],
    ) -> None:
        with self.file_lock(), self.connection() as conn:
            self.init_schema(conn)
            conn.execute(
                """
                INSERT INTO injection_runs(
                    injection_id, phase, fault_type, round_id, job_id, card_id,
                    status, error_code, result_json, run_count, updated_wall
                ) VALUES(?, ?, ?, ?, ?, ?, 'triggered', ?, ?, 1, ?)
                ON CONFLICT(injection_id) DO UPDATE SET
                    round_id=excluded.round_id,
                    job_id=excluded.job_id,
                    card_id=excluded.card_id,
                    status='triggered',
                    error_code=excluded.error_code,
                    result_json=excluded.result_json,
                    run_count=injection_runs.run_count+1,
                    updated_wall=excluded.updated_wall
                """,
                (
                    injection_id,
                    phase,
                    fault_type,
                    round_id,
                    job_id,
                    card_id,
                    error_code,
                    canonical_json(result),
                    utc_wall_now(),
                ),
            )

    def list_pending_operations(self, conn: sqlite3.Connection):
        return conn.execute(
            """
            SELECT * FROM operations
             WHERE status IN ('planned', 'applied')
             ORDER BY rowid ASC
            """
        ).fetchall()

    def list_events(self, conn: sqlite3.Connection, limit: int = 1000):
        return conn.execute(
            "SELECT * FROM audit_events ORDER BY event_id DESC LIMIT ?",
            (limit,),
        ).fetchall()
