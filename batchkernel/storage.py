from __future__ import annotations

import contextlib
import fcntl
import sqlite3
from pathlib import Path
from typing import Iterator

from .errors import BusinessError

SCHEMA_VERSION = 1
DB_FILENAME = "scheduler.sqlite3"
LOCK_FILENAME = "scheduler.lock"


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_versions (
    version INTEGER PRIMARY KEY,
    content_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL,
    action TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_current (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL REFERENCES replica_versions(version)
);

CREATE TABLE IF NOT EXISTS requests (
    request_seq INTEGER PRIMARY KEY,
    arrival_ms INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    declared_replica_id TEXT,
    replica_id TEXT NOT NULL,
    state TEXT NOT NULL,
    failure_count INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    remaining_failures INTEGER NOT NULL DEFAULT 0,
    window_key TEXT,
    batch_key TEXT,
    completion_ms INTEGER,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS windows (
    window_key TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    window_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('planned', 'applying', 'committed')),
    plan_json TEXT NOT NULL,
    planned_clock_ms INTEGER NOT NULL,
    committed_clock_ms INTEGER,
    UNIQUE (replica_id, window_index)
);

CREATE TABLE IF NOT EXISTS window_requests (
    window_key TEXT NOT NULL REFERENCES windows(window_key),
    request_seq INTEGER NOT NULL REFERENCES requests(request_seq),
    batch_index INTEGER,
    ordinal INTEGER NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('batch', 'expired')),
    PRIMARY KEY (window_key, request_seq),
    UNIQUE (request_seq)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_key TEXT PRIMARY KEY,
    window_key TEXT NOT NULL REFERENCES windows(window_key),
    batch_index INTEGER NOT NULL,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('planned', 'committed')),
    executed INTEGER NOT NULL DEFAULT 0,
    compute_fixed INTEGER NOT NULL,
    memory_fixed INTEGER NOT NULL,
    queue_fixed INTEGER NOT NULL,
    compute_raw REAL NOT NULL,
    memory_raw REAL NOT NULL,
    queue_raw REAL NOT NULL,
    compute_error_bound REAL NOT NULL,
    memory_error_bound REAL NOT NULL,
    queue_error_bound REAL NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    decision_json TEXT NOT NULL,
    estimated_completion_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    UNIQUE (window_key, batch_index)
);

CREATE TABLE IF NOT EXISTS batch_requests (
    batch_key TEXT NOT NULL REFERENCES batches(batch_key),
    request_seq INTEGER NOT NULL REFERENCES requests(request_seq),
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (batch_key, request_seq),
    UNIQUE (request_seq)
);

CREATE TABLE IF NOT EXISTS precision_records (
    record_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL REFERENCES batches(batch_key),
    window_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    scale INTEGER NOT NULL,
    raw_value REAL NOT NULL,
    fixed_value INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    quantization_error_bound REAL NOT NULL,
    arithmetic_error_bound REAL NOT NULL,
    total_error_bound REAL NOT NULL,
    error_budget REAL NOT NULL,
    within_error_budget INTEGER NOT NULL,
    source_chain_json TEXT NOT NULL,
    operation_order_json TEXT NOT NULL,
    aggregate_order_json TEXT NOT NULL,
    UNIQUE (batch_key, metric)
);

CREATE TABLE IF NOT EXISTS sensitive_decisions (
    decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key TEXT NOT NULL REFERENCES batches(batch_key),
    window_key TEXT NOT NULL,
    decision TEXT NOT NULL,
    metric TEXT NOT NULL,
    tolerance REAL NOT NULL,
    left_json TEXT NOT NULL,
    right_json TEXT NOT NULL,
    absolute_gap REAL NOT NULL,
    source_chain_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    clock_ms INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    replica_id TEXT,
    replica_version INTEGER,
    window_key TEXT,
    batch_key TEXT,
    request_seq INTEGER,
    from_state TEXT,
    to_state TEXT,
    failure_count_before INTEGER,
    failure_count_after INTEGER,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_watermarks (
    replica_id TEXT PRIMARY KEY,
    current_version INTEGER NOT NULL,
    next_window_index INTEGER NOT NULL,
    next_window_start_ms INTEGER NOT NULL,
    next_window_end_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_usage (
    replica_id TEXT PRIMARY KEY,
    replica_version INTEGER NOT NULL,
    compute_fixed INTEGER NOT NULL DEFAULT 0,
    memory_fixed INTEGER NOT NULL DEFAULT 0,
    updated_clock_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_windows_state ON windows(state, replica_id, window_index);
CREATE INDEX IF NOT EXISTS idx_requests_state ON requests(state, replica_id, arrival_ms);
CREATE INDEX IF NOT EXISTS idx_batches_window ON batches(window_key, batch_index);
CREATE INDEX IF NOT EXISTS idx_batches_execution ON batches(executed, state, estimated_completion_ms);
CREATE INDEX IF NOT EXISTS idx_ledger_request ON ledger(request_seq, event_id);
"""


def db_path(state_dir: str | Path) -> Path:
    return Path(state_dir) / DB_FILENAME


def state_exists(state_dir: str | Path) -> bool:
    return db_path(state_dir).is_file()


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path),
        timeout=15.0,
        isolation_level=None,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 15000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def open_database(state_dir: str | Path, create: bool = False) -> sqlite3.Connection:
    path = db_path(state_dir)
    if not path.is_file():
        if not create:
            raise BusinessError(
                "E_STATE_NOT_FOUND",
                "scheduler state database does not exist",
                {"state_dir": str(state_dir), "database": str(path)},
            )
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = _connect(path)
    if create:
        initialize_schema(connection)
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            "BEGIN IMMEDIATE;\n"
            + SCHEMA_SQL
            + "\nINSERT OR REPLACE INTO meta(key, value) "
            "VALUES('schema_version', "
            + repr(str(SCHEMA_VERSION))
            + ");\nCOMMIT;"
        )
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


@contextlib.contextmanager
def transaction(
    connection: sqlite3.Connection,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


@contextlib.contextmanager
def file_lock(state_dir: str | Path, exclusive: bool = True) -> Iterator[None]:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / LOCK_FILENAME
    with lock_path.open("a+b") as handle:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(handle.fileno(), operation)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def reset_database(state_dir: str | Path) -> None:
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    for name in (
        DB_FILENAME,
        f"{DB_FILENAME}-wal",
        f"{DB_FILENAME}-shm",
        f"{DB_FILENAME}-journal",
    ):
        path = directory / name
        try:
            path.unlink()
        except FileNotFoundError:
            pass


def get_meta(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def set_meta(connection: sqlite3.Connection, key: str, value: str | int | None) -> None:
    if value is None:
        connection.execute("DELETE FROM meta WHERE key = ?", (key,))
    else:
        connection.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def append_ledger(
    connection: sqlite3.Connection,
    *,
    clock_ms: int,
    event_type: str,
    detail: dict | list | None = None,
    replica_id: str | None = None,
    replica_version: int | None = None,
    window_key: str | None = None,
    batch_key: str | None = None,
    request_seq: int | None = None,
    from_state: str | None = None,
    to_state: str | None = None,
    failure_count_before: int | None = None,
    failure_count_after: int | None = None,
) -> None:
    from .config import canonical_json

    connection.execute(
        """
        INSERT INTO ledger(
            clock_ms, event_type, replica_id, replica_version, window_key,
            batch_key, request_seq, from_state, to_state,
            failure_count_before, failure_count_after, detail_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            clock_ms,
            event_type,
            replica_id,
            replica_version,
            window_key,
            batch_key,
            request_seq,
            from_state,
            to_state,
            failure_count_before,
            failure_count_after,
            canonical_json(detail or {}),
        ),
    )

