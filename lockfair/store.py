"""SQLite persistence, snapshots and cross-platform orchestration locks."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import ErrorCode, error


SCHEMA_VERSION = 1


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def state_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def orchestrator_lock(db_path: str | Path) -> Iterator[None]:
    """An advisory inter-process lock that works on POSIX and Windows."""

    path = Path(db_path)
    _ensure_parent(path)
    lock_path = path.with_suffix(path.suffix + ".orchestrator.lock")
    handle = lock_path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    _ensure_parent(path)
    conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version INTEGER NOT NULL UNIQUE,
    round_no INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    kind TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    state_json TEXT NOT NULL,
    request_id TEXT,
    state_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_round ON snapshots(round_no, version DESC);

CREATE TABLE IF NOT EXISTS current_snapshot (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS resources (
    lock_key TEXT PRIMARY KEY,
    lease_duration_ms INTEGER NOT NULL,
    lease_generation INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    max_wait_rounds INTEGER NOT NULL,
    active_seq INTEGER,
    active_holder TEXT,
    active_expires_ms INTEGER,
    state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS holders (
    identity TEXT PRIMARY KEY,
    heartbeat_ms INTEGER NOT NULL,
    session_generation INTEGER NOT NULL,
    max_concurrent_grants INTEGER NOT NULL,
    occupied_count INTEGER NOT NULL CHECK (occupied_count >= 0),
    state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    seq INTEGER PRIMARY KEY,
    holder TEXT NOT NULL,
    lock_key TEXT NOT NULL,
    arrival_ms INTEGER NOT NULL,
    desired_hold_ms INTEGER NOT NULL,
    fairness_label TEXT NOT NULL,
    priority INTEGER NOT NULL,
    expected_lease_generation INTEGER,
    status TEXT NOT NULL,
    enqueued_round INTEGER NOT NULL,
    queue_position INTEGER,
    wait_rounds INTEGER NOT NULL,
    forced_yields INTEGER NOT NULL,
    deferred_rounds INTEGER NOT NULL,
    grant_round INTEGER,
    lease_generation INTEGER,
    grant_start_ms INTEGER,
    expires_at_ms INTEGER,
    state_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applications_queue
    ON applications(lock_key, status, fairness_label, arrival_ms, seq);

CREATE TABLE IF NOT EXISTS grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    lock_key TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    holder TEXT NOT NULL,
    start_clock_ms INTEGER NOT NULL,
    expires_at_ms INTEGER NOT NULL,
    cost_ms INTEGER NOT NULL,
    forced_yield INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    UNIQUE(lock_key, lease_generation),
    UNIQUE(round_no, seq)
);
CREATE INDEX IF NOT EXISTS idx_grants_status ON grants(status, lock_key);

CREATE TABLE IF NOT EXISTS round_jobs (
    request_id TEXT PRIMARY KEY,
    round_no INTEGER NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    base_version INTEGER,
    base_snapshot_id INTEGER,
    payload_json TEXT NOT NULL,
    result_version INTEGER,
    error_json TEXT,
    state_version INTEGER
);
CREATE INDEX IF NOT EXISTS idx_round_jobs_status ON round_jobs(status, round_no);

CREATE TABLE IF NOT EXISTS rounds (
    round_no INTEGER PRIMARY KEY,
    logical_clock_ms INTEGER NOT NULL,
    round_budget_ms INTEGER NOT NULL,
    snapshot_version INTEGER NOT NULL,
    request_id TEXT,
    request_hash TEXT,
    summary_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
    idempotency_key TEXT PRIMARY KEY,
    round_no INTEGER NOT NULL,
    kind TEXT NOT NULL,
    seq INTEGER,
    lock_key TEXT,
    payload_json TEXT NOT NULL,
    state_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS queue_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    lock_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    queue_position INTEGER,
    wait_rounds INTEGER NOT NULL,
    fairness_label TEXT NOT NULL,
    details_json TEXT NOT NULL,
    state_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_queue_events_round ON queue_events(round_no, seq);

CREATE TABLE IF NOT EXISTS round_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_no INTEGER NOT NULL,
    code TEXT NOT NULL,
    lock_key TEXT,
    application_seq INTEGER,
    constraint_name TEXT,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    state_version INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_round_errors_round ON round_errors(round_no, code);

CREATE TABLE IF NOT EXISTS recovery_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    action TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    state_version INTEGER
);
"""


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


def database_initialized(db_path: str | Path) -> bool:
    path = Path(db_path)
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(str(path), timeout=5.0)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='current_snapshot'"
            ).fetchone()
            if not row:
                return False
            return conn.execute("SELECT 1 FROM current_snapshot WHERE singleton=1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False


def reset_database_files(db_path: str | Path) -> None:
    path = Path(db_path)
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _read_state_json(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["state_json"])


def read_snapshot(
    conn: sqlite3.Connection,
    *,
    snapshot_id: int | None = None,
    round_no: int | None = None,
    version: int | None = None,
) -> dict[str, Any]:
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN")
    try:
        if snapshot_id is not None:
            row = conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        elif version is not None:
            row = conn.execute("SELECT * FROM snapshots WHERE version=?", (version,)).fetchone()
        elif round_no is not None:
            row = conn.execute(
                "SELECT * FROM snapshots WHERE round_no=? ORDER BY version DESC LIMIT 1",
                (round_no,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT s.* FROM current_snapshot c
                JOIN snapshots s ON s.id=c.snapshot_id
                WHERE c.singleton=1
                """
            ).fetchone()
        if row is None:
            if owns_transaction:
                conn.execute("ROLLBACK")
            raise error(ErrorCode.STATE_NOT_INITIALIZED, "不存在可读取的已提交快照")
        result = {
            "snapshot_id": row["id"],
            "version": row["version"],
            "round_no": row["round_no"],
            "logical_clock_ms": row["logical_clock_ms"],
            "kind": row["kind"],
            "state_hash": row["state_hash"],
            "request_id": row["request_id"],
            "state_version": row["state_version"],
            "state": _read_state_json(row),
        }
        if owns_transaction:
            conn.execute("COMMIT")
        return result
    except Exception:
        if owns_transaction and conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def read_current_pointer(conn: sqlite3.Connection) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT version, snapshot_id FROM current_snapshot WHERE singleton=1"
    ).fetchone()
    if row is None:
        return None
    return int(row["version"]), int(row["snapshot_id"])


def _write_normalized(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    version = int(state["state_version"])
    conn.execute("DELETE FROM resources")
    conn.execute("DELETE FROM holders")
    conn.execute("DELETE FROM applications")

    for lock_key, resource in state["resources"].items():
        active = resource.get("active") or {}
        conn.execute(
            """
            INSERT INTO resources(
                lock_key, lease_duration_ms, lease_generation, priority,
                max_wait_rounds, active_seq, active_holder, active_expires_ms,
                state_version
            ) VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                lock_key,
                resource["lease_duration_ms"],
                resource["lease_generation"],
                resource["priority"],
                resource["max_wait_rounds"],
                active.get("seq"),
                active.get("holder"),
                active.get("expires_at_ms"),
                version,
            ),
        )

    for identity, holder in state["holders"].items():
        conn.execute(
            """
            INSERT INTO holders(
                identity, heartbeat_ms, session_generation, max_concurrent_grants,
                occupied_count, state_version
            ) VALUES(?,?,?,?,?,?)
            """,
            (
                identity,
                holder["heartbeat_ms"],
                holder["session_generation"],
                holder["max_concurrent_grants"],
                holder["occupied_count"],
                version,
            ),
        )

    for seq in sorted(state["applications"], key=lambda item: int(item)):
        app = state["applications"][str(seq)]
        conn.execute(
            """
            INSERT INTO applications(
                seq, holder, lock_key, arrival_ms, desired_hold_ms,
                fairness_label, priority, expected_lease_generation, status,
                enqueued_round, queue_position, wait_rounds, forced_yields,
                deferred_rounds, grant_round, lease_generation, grant_start_ms,
                expires_at_ms, state_version
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                app["seq"],
                app["holder"],
                app["lock_key"],
                app["arrival_ms"],
                app["desired_hold_ms"],
                app["fairness_label"],
                app["priority"],
                app.get("expected_lease_generation"),
                app["status"],
                app["enqueued_round"],
                app.get("queue_position"),
                app["wait_rounds"],
                app["forced_yields"],
                app["deferred_rounds"],
                app.get("grant_round"),
                app.get("lease_generation"),
                app.get("grant_start_ms"),
                app.get("expires_at_ms"),
                version,
            ),
        )


def write_snapshot(
    conn: sqlite3.Connection,
    state: dict[str, Any],
    *,
    kind: str,
    request_id: str | None,
    summary: dict[str, Any] | None = None,
) -> tuple[int, int]:
    """Write a complete committed snapshot and move the pointer atomically.

    The caller must already be inside an active SQLite write transaction.
    """

    version = int(state["state_version"])
    digest = state_hash(state)
    cursor = conn.execute(
        """
        INSERT INTO snapshots(
            version, round_no, logical_clock_ms, kind, state_hash,
            state_json, request_id, state_version
        ) VALUES(?,?,?,?,?,?,?,?)
        """,
        (
            version,
            int(state["round_no"]),
            int(state["logical_clock_ms"]),
            kind,
            digest,
            canonical_json(state),
            request_id,
            version,
        ),
    )
    snapshot_id = int(cursor.lastrowid)
    conn.execute(
        """
        INSERT INTO current_snapshot(singleton, snapshot_id, version)
        VALUES(1, ?, ?)
        ON CONFLICT(singleton) DO UPDATE SET
            snapshot_id=excluded.snapshot_id,
            version=excluded.version
        """,
        (snapshot_id, version),
    )
    _write_normalized(conn, state)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('current_version', ?)",
        (str(version),),
    )

    if summary is not None:
        if kind == "round":
            round_summary = {
                key: value
                for key, value in summary.items()
                if key not in {"actions", "queue_events"}
            }
            conn.execute(
                """
                INSERT INTO rounds(
                    round_no, logical_clock_ms, round_budget_ms, snapshot_version,
                    request_id, request_hash, summary_json
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    int(state["round_no"]),
                    int(state["logical_clock_ms"]),
                    int(state["config"]["round_budget_ms"]),
                    version,
                    request_id,
                    summary.get("request_hash"),
                    canonical_json(round_summary),
                ),
            )
        for action in summary.get("actions", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO actions(
                    idempotency_key, round_no, kind, seq, lock_key,
                    payload_json, state_version
                ) VALUES(?,?,?,?,?,?,?)
                """,
                (
                    action["idempotency_key"],
                    int(state["round_no"]),
                    action["kind"],
                    action.get("seq"),
                    action.get("lock_key"),
                    canonical_json(action.get("payload", {})),
                    version,
                ),
            )
        for event in summary.get("queue_events", []):
            conn.execute(
                """
                INSERT INTO queue_events(
                    round_no, seq, lock_key, event_type, queue_position,
                    wait_rounds, fairness_label, details_json, state_version
                ) VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(state["round_no"]),
                    int(event["seq"]),
                    event["lock_key"],
                    event["event_type"],
                    event.get("queue_position"),
                    int(event.get("wait_rounds", 0)),
                    event["fairness_label"],
                    canonical_json(event.get("details", {})),
                    version,
                ),
            )
        for item in summary.get("errors", []):
            conn.execute(
                """
                INSERT INTO round_errors(
                    round_no, code, lock_key, application_seq, constraint_name,
                    message, details_json, state_version
                ) VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    int(state["round_no"]),
                    item["code"],
                    item.get("lock_key"),
                    item.get("application_seq"),
                    item.get("constraint"),
                    item["message"],
                    canonical_json(item.get("details", {})),
                    version,
                ),
            )
        for grant in summary.get("grants", []):
            conn.execute(
                """
                INSERT OR IGNORE INTO grants(
                    round_no, seq, lock_key, lease_generation, holder,
                    start_clock_ms, expires_at_ms, cost_ms, forced_yield,
                    status, state_version
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    int(state["round_no"]),
                    int(grant["seq"]),
                    grant["lock_key"],
                    int(grant["lease_generation"]),
                    grant["holder"],
                    int(grant["start_clock_ms"]),
                    int(grant["expires_at_ms"]),
                    int(grant["cost_ms"]),
                    1 if grant.get("forced_yield") else 0,
                    grant.get("status", "active"),
                    version,
                ),
            )
        for expired in summary.get("expired", []):
            conn.execute(
                """
                UPDATE grants SET status='expired', state_version=?
                WHERE lock_key=? AND lease_generation=?
                """,
                (version, expired["lock_key"], int(expired["lease_generation"])),
            )

    return snapshot_id, version


def save_job_prepared(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    round_no: int,
    request_hash: str,
    base_version: int,
    base_snapshot_id: int,
    payload: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO round_jobs(
            request_id, round_no, request_hash, status, base_version,
            base_snapshot_id, payload_json, state_version
        ) VALUES(?,?,?,'prepared',?,?,?,?)
        ON CONFLICT(request_id) DO NOTHING
        """,
        (
            request_id,
            round_no,
            request_hash,
            base_version,
            base_snapshot_id,
            canonical_json(payload),
            base_version,
        ),
    )


def load_job(conn: sqlite3.Connection, request_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM round_jobs WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["payload"] = json.loads(data.pop("payload_json"))
    if data.get("error_json"):
        data["error"] = json.loads(data["error_json"])
    return data


def complete_job(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    version: int,
    status: str = "completed",
    error_payload: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        """
        UPDATE round_jobs SET status=?, result_version=?, error_json=?, state_version=?
        WHERE request_id=?
        """,
        (
            status,
            version,
            canonical_json(error_payload) if error_payload is not None else None,
            version,
            request_id,
        ),
    )


def log_recovery(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    action: str,
    detail: dict[str, Any],
    version: int | None,
) -> None:
    conn.execute(
        "INSERT INTO recovery_log(request_id, action, detail_json, state_version) VALUES(?,?,?,?)",
        (request_id, action, canonical_json(detail), version),
    )


def list_prepared_jobs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM round_jobs WHERE status='prepared' ORDER BY round_no, request_id"
    ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        data["payload"] = json.loads(data.pop("payload_json"))
        result.append(data)
    return result


def list_rounds(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT round_no, logical_clock_ms, round_budget_ms, snapshot_version,
               request_id, summary_json
        FROM rounds ORDER BY round_no
        """
    ).fetchall()
    return [
        {
            "round_no": row["round_no"],
            "logical_clock_ms": row["logical_clock_ms"],
            "round_budget_ms": row["round_budget_ms"],
            "snapshot_version": row["snapshot_version"],
            "request_id": row["request_id"],
            "summary": json.loads(row["summary_json"]),
        }
        for row in rows
    ]


def list_errors(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT round_no, code, lock_key, application_seq, constraint_name,
               message, details_json, state_version
        FROM round_errors ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "round_no": row["round_no"],
            "code": row["code"],
            "lock_key": row["lock_key"],
            "application_seq": row["application_seq"],
            "constraint": row["constraint_name"],
            "message": row["message"],
            "details": json.loads(row["details_json"]),
            "state_version": row["state_version"],
        }
        for row in rows
    ]


def list_queue_events(
    conn: sqlite3.Connection,
    *,
    round_no: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    if round_no is None:
        rows = conn.execute(
            """
            SELECT round_no, seq, lock_key, event_type, queue_position,
                   wait_rounds, fairness_label, details_json, state_version
            FROM queue_events ORDER BY id DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT round_no, seq, lock_key, event_type, queue_position,
                   wait_rounds, fairness_label, details_json, state_version
            FROM queue_events WHERE round_no=? ORDER BY id ASC LIMIT ?
            """,
            (round_no, limit),
        ).fetchall()
    return [
        {
            "round_no": row["round_no"],
            "seq": row["seq"],
            "lock_key": row["lock_key"],
            "event_type": row["event_type"],
            "queue_position": row["queue_position"],
            "wait_rounds": row["wait_rounds"],
            "fairness_label": row["fairness_label"],
            "details": json.loads(row["details_json"]),
            "state_version": row["state_version"],
        }
        for row in rows
    ]


def list_recovery_log(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT request_id, action, detail_json, state_version
        FROM recovery_log ORDER BY id DESC LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [
        {
            "request_id": row["request_id"],
            "action": row["action"],
            "detail": json.loads(row["detail_json"]),
            "state_version": row["state_version"],
        }
        for row in rows
    ]
