"""SQLite persistence, transactions, and a cross-platform file lock."""

from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import ColAggError, E_NO_STATE, E_STORAGE


SCHEMA_VERSION = "1"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    version INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    query_json TEXT NOT NULL,
    precision_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    classification TEXT NOT NULL,
    created_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS current_plan (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL,
    digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_ops (
    version INTEGER NOT NULL,
    operator_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    PRIMARY KEY (version, operator_id)
);

CREATE TABLE IF NOT EXISTS plan_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    old_version INTEGER,
    new_version INTEGER NOT NULL,
    old_digest TEXT,
    new_digest TEXT NOT NULL,
    classification TEXT NOT NULL,
    reused_json TEXT NOT NULL,
    rebuilt_json TEXT NOT NULL,
    clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    seq INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    provided_digest TEXT,
    row_count INTEGER NOT NULL,
    columns_json TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    clock INTEGER,
    loaded_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operator_state (
    operator_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    watermark_seq INTEGER NOT NULL,
    rows_seen INTEGER NOT NULL,
    primary_path TEXT NOT NULL,
    status TEXT NOT NULL,
    failure_code TEXT,
    failure_message TEXT,
    failure_batch_seq INTEGER,
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    finalized INTEGER NOT NULL DEFAULT 0,
    updated_clock INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS applied (
    operator_id TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    result_digest TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    clock INTEGER NOT NULL,
    PRIMARY KEY (operator_id, batch_seq)
);

CREATE TABLE IF NOT EXISTS batch_outputs (
    operator_id TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    plan_version INTEGER NOT NULL,
    output_json TEXT NOT NULL,
    output_digest TEXT NOT NULL,
    PRIMARY KEY (operator_id, batch_seq)
);

CREATE TABLE IF NOT EXISTS group_results (
    operator_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    group_json TEXT NOT NULL,
    count INTEGER NOT NULL,
    watermark_seq INTEGER NOT NULL,
    primary_path TEXT NOT NULL,
    value_number REAL,
    float_value REAL NOT NULL,
    fixed_value REAL NOT NULL,
    error_bound REAL NOT NULL,
    path_delta REAL NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    sensitivity_json TEXT NOT NULL,
    error_chain_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    PRIMARY KEY (operator_id, group_key)
);

CREATE TABLE IF NOT EXISTS window_results (
    operator_id TEXT NOT NULL,
    group_key TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    start_row INTEGER NOT NULL,
    end_row INTEGER NOT NULL,
    length INTEGER NOT NULL,
    complete INTEGER NOT NULL,
    status TEXT NOT NULL,
    primary_path TEXT NOT NULL,
    value_number REAL,
    float_value REAL NOT NULL,
    fixed_value REAL NOT NULL,
    error_bound REAL NOT NULL,
    path_delta REAL NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    sensitivity_json TEXT NOT NULL,
    error_chain_json TEXT NOT NULL,
    record_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    PRIMARY KEY (operator_id, group_key, window_index)
);

CREATE TABLE IF NOT EXISTS conversion_traces (
    operator_id TEXT NOT NULL,
    batch_seq INTEGER NOT NULL,
    column_name TEXT NOT NULL,
    row_index INTEGER NOT NULL,
    path TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    scale INTEGER NOT NULL,
    scaled_value TEXT NOT NULL,
    direction TEXT NOT NULL,
    group_key TEXT NOT NULL,
    details_json TEXT NOT NULL,
    PRIMARY KEY (operator_id, batch_seq, column_name, row_index, path)
);

CREATE TABLE IF NOT EXISTS failures (
    failure_id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    batch_seq INTEGER,
    operator_id TEXT NOT NULL DEFAULT '',
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    clock INTEGER NOT NULL,
    UNIQUE (operator_id, batch_seq, code)
);

CREATE INDEX IF NOT EXISTS idx_applied_batch ON applied(batch_seq);
CREATE INDEX IF NOT EXISTS idx_group_results_operator ON group_results(operator_id);
CREATE INDEX IF NOT EXISTS idx_window_results_operator ON window_results(operator_id);
CREATE INDEX IF NOT EXISTS idx_failures_operator ON failures(operator_id);
"""


class FileLock:
    """A small advisory lock that works on POSIX and Windows."""

    def __init__(self, path: Path, timeout: float = 30.0) -> None:
        self.path = path
        self.timeout = timeout
        self._handle = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self._handle.close()
                    self._handle = None
                    raise ColAggError(
                        E_STORAGE,
                        f"timed out waiting for file lock: {self.path}",
                    )
                time.sleep(0.02)

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', ?)",
        (SCHEMA_VERSION,),
    )
    connection.commit()


def open_database(path: Path, *, create: bool = False) -> sqlite3.Connection:
    path = path.resolve()
    if not path.exists() and not create:
        raise ColAggError(E_NO_STATE, f"state database does not exist: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(path),
        isolation_level=None,
        timeout=30.0,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    if create:
        initialize_schema(connection)
    else:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.close()
            raise ColAggError(E_NO_STATE, f"database is not initialized: {path}")
    return connection


@contextmanager
def immediate_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


@contextmanager
def read_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.execute("COMMIT")


def database_lock_path(database_path: Path) -> Path:
    return database_path.with_name(database_path.name + ".lock")


def current_plan_version(connection: sqlite3.Connection) -> tuple[int, str] | None:
    row = connection.execute(
        "SELECT version, digest FROM current_plan WHERE singleton = 1"
    ).fetchone()
    if row is None:
        return None
    return int(row["version"]), str(row["digest"])


def max_batch_clock(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT MAX(clock) AS maximum_clock FROM batches WHERE clock IS NOT NULL"
    ).fetchone()
    return int(row["maximum_clock"] or 0)

