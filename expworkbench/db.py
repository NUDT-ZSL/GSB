from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Iterator

from . import SCHEMA_VERSION
from .errors import E_DB_BUSY, E_NOT_LOADED, E_STATE_CORRUPT, WorkbenchError, fail


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    hyperparameters_json TEXT NOT NULL,
    dataset_version TEXT NOT NULL,
    code_commit TEXT NOT NULL,
    baseline_metrics_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    metric_references_json TEXT NOT NULL,
    declaration_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    aliases_json TEXT NOT NULL,
    data_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS experiment_fingerprint_history (
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    fingerprint TEXT NOT NULL,
    declaration_json TEXT NOT NULL,
    first_data_epoch INTEGER NOT NULL,
    PRIMARY KEY(experiment_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS metric_records (
    record_id TEXT PRIMARY KEY,
    seq INTEGER NOT NULL UNIQUE,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    metric TEXT NOT NULL,
    step INTEGER NOT NULL,
    value_text TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    record_digest TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    UNIQUE(experiment_id, metric, step)
);
CREATE INDEX IF NOT EXISTS metric_records_stream
    ON metric_records(experiment_id, metric, logical_clock_ms, seq);

CREATE TABLE IF NOT EXISTS snapshots (
    snapshot_version TEXT PRIMARY KEY,
    version_number INTEGER NOT NULL,
    version_key_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    trace_generation INTEGER NOT NULL,
    trace_generation_floor INTEGER NOT NULL,
    package_digest TEXT NOT NULL,
    classification TEXT NOT NULL,
    source_summary_json TEXT NOT NULL,
    previous_snapshot_version TEXT,
    created_data_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_sources (
    snapshot_version TEXT NOT NULL REFERENCES snapshots(snapshot_version),
    source_format TEXT NOT NULL,
    source_index INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_clock_ms INTEGER NOT NULL,
    trace_generation INTEGER NOT NULL,
    shard_hash TEXT NOT NULL,
    priority INTEGER NOT NULL,
    raw_summary_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_version, source_format, source_index)
);

CREATE TABLE IF NOT EXISTS snapshot_source_entries (
    snapshot_version TEXT NOT NULL REFERENCES snapshots(snapshot_version),
    source_format TEXT NOT NULL,
    entry_key TEXT NOT NULL,
    source_version TEXT NOT NULL,
    alias TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    relative_refresh_seconds INTEGER,
    absolute_refresh_at_ms INTEGER NOT NULL,
    source_clock_ms INTEGER NOT NULL,
    trace_generation INTEGER NOT NULL,
    source_priority INTEGER NOT NULL,
    metric_digest TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    conversion_scale INTEGER NOT NULL,
    conversion_remainder_ms INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    raw_time_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_version, source_format, entry_key)
);
CREATE INDEX IF NOT EXISTS snapshot_source_entries_target
    ON snapshot_source_entries(snapshot_version, experiment_id, metric);

CREATE TABLE IF NOT EXISTS snapshot_view_entries (
    snapshot_version TEXT NOT NULL REFERENCES snapshots(snapshot_version),
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    alias TEXT NOT NULL,
    selected_source_format TEXT NOT NULL,
    selected_source_version TEXT NOT NULL,
    selected_entry_key TEXT NOT NULL,
    selected_clock_ms INTEGER NOT NULL,
    selected_generation INTEGER NOT NULL,
    selected_priority INTEGER NOT NULL,
    relative_refresh_seconds INTEGER,
    absolute_refresh_at_ms INTEGER NOT NULL,
    conversion_scale INTEGER NOT NULL,
    conversion_remainder_ms INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    metric_digest TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    raw_time_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    PRIMARY KEY(snapshot_version, experiment_id, metric)
);

CREATE TABLE IF NOT EXISTS windows (
    snapshot_version TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    window_number INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    sum_text TEXT NOT NULL,
    mean_text TEXT NOT NULL,
    min_text TEXT NOT NULL,
    max_text TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    summary_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    lineage_fingerprint TEXT NOT NULL,
    last_event_id TEXT,
    updated_data_epoch INTEGER NOT NULL,
    PRIMARY KEY(snapshot_version, experiment_id, metric, window_start_ms, window_end_ms)
);
CREATE INDEX IF NOT EXISTS windows_metric
    ON windows(snapshot_version, experiment_id, metric, window_start_ms);

CREATE TABLE IF NOT EXISTS window_record_assignments (
    snapshot_version TEXT NOT NULL,
    record_id TEXT NOT NULL REFERENCES metric_records(record_id),
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    PRIMARY KEY(snapshot_version, record_id)
);

CREATE TABLE IF NOT EXISTS refresh_watermarks (
    snapshot_version TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    last_clock_ms INTEGER NOT NULL,
    updated_data_epoch INTEGER NOT NULL,
    PRIMARY KEY(snapshot_version, experiment_id, metric)
);

CREATE TABLE IF NOT EXISTS metric_summaries (
    snapshot_version TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    window_count INTEGER NOT NULL,
    record_count INTEGER NOT NULL,
    sum_text TEXT NOT NULL,
    mean_text TEXT NOT NULL,
    min_text TEXT NOT NULL,
    max_text TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    summary_digest TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    lineage_fingerprint TEXT NOT NULL,
    last_event_id TEXT,
    updated_data_epoch INTEGER NOT NULL,
    PRIMARY KEY(snapshot_version, experiment_id, metric)
);

CREATE TABLE IF NOT EXISTS baseline_comparisons (
    snapshot_version TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    window_number INTEGER NOT NULL,
    baseline_value_text TEXT,
    value_text TEXT NOT NULL,
    delta_text TEXT,
    abs_delta_text TEXT,
    relative_delta_text TEXT,
    direction TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    comparison_digest TEXT NOT NULL,
    comparison_json TEXT NOT NULL,
    updated_data_epoch INTEGER NOT NULL,
    PRIMARY KEY(snapshot_version, experiment_id, metric, window_start_ms, window_end_ms)
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    semantics_digest TEXT NOT NULL,
    now_ms INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    operations_json TEXT NOT NULL,
    invalidated_json TEXT NOT NULL,
    status TEXT NOT NULL,
    applied_data_epoch INTEGER NOT NULL,
    event_id TEXT
);

CREATE TABLE IF NOT EXISTS invalidation_events (
    event_id TEXT PRIMARY KEY,
    patch_id TEXT NOT NULL REFERENCES patches(patch_id),
    snapshot_version TEXT NOT NULL,
    data_epoch INTEGER NOT NULL,
    now_ms INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    trigger_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_order INTEGER NOT NULL,
    completed_order INTEGER
);

CREATE TABLE IF NOT EXISTS invalidation_items (
    event_id TEXT NOT NULL REFERENCES invalidation_events(event_id),
    experiment_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    reason TEXT NOT NULL,
    source_chain_json TEXT NOT NULL,
    rejected_candidates_json TEXT NOT NULL,
    status TEXT NOT NULL,
    PRIMARY KEY(event_id, experiment_id, metric)
);

CREATE TABLE IF NOT EXISTS refresh_runs (
    run_id TEXT PRIMARY KEY,
    snapshot_version TEXT NOT NULL,
    data_epoch INTEGER NOT NULL,
    now_ms INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    input_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT NOT NULL
);
"""


def database_exists(path: str) -> bool:
    return os.path.isfile(path) and os.path.getsize(path) > 0


def connect(path: str, readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        connection = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=30,
            isolation_level=None,
        )
    else:
        connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    if not readonly:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
    return connection


def initialize(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    connection = connect(path)
    try:
        connection.executescript(SCHEMA_SQL)
        with transaction(connection):
            defaults = {
                "schema_version": str(SCHEMA_VERSION),
                "logical_clock_watermark_ms": "0",
                "data_epoch": "0",
                "event_sequence": "0",
            }
            for key, value in defaults.items():
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (key, value)
                )
    finally:
        connection.close()


def open_existing(
    path: str, readonly: bool = False, require_active: bool = True
) -> sqlite3.Connection:
    if not database_exists(path):
        fail(E_NOT_LOADED, "Workbench database does not exist", path=path)
    connection = connect(path, readonly=readonly)
    try:
        row = connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None or row["value"] != str(SCHEMA_VERSION):
            fail(
                E_STATE_CORRUPT,
                "Workbench database schema version is unsupported",
                expected=SCHEMA_VERSION,
                actual=None if row is None else row["value"],
            )
        if require_active and get_meta(connection, "active_snapshot_version") is None:
            fail(E_NOT_LOADED, "No tracking snapshot has been loaded")
    except sqlite3.Error as exc:
        fail(E_STATE_CORRUPT, "Unable to read workbench database", detail=str(exc))
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            fail(E_DB_BUSY, "SQLite database is busy", detail=str(exc))
        raise
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


@contextlib.contextmanager
def read_transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN")
    try:
        yield connection
    finally:
        connection.execute("ROLLBACK")


def get_meta(connection: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return default if row is None else str(row["value"])


def get_meta_int(connection: sqlite3.Connection, key: str, default: int = 0) -> int:
    value = get_meta(connection, key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        fail(E_STATE_CORRUPT, "Stored metadata is not an integer", key=key, value=value)


def set_meta(connection: sqlite3.Connection, key: str, value: str | int) -> None:
    connection.execute(
        """
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, str(value)),
    )


def allocate_sequence(connection: sqlite3.Connection, key: str) -> int:
    current = get_meta_int(connection, key)
    next_value = current + 1
    set_meta(connection, key, next_value)
    return next_value


class FileLock:
    """Cross-platform advisory exclusive lock for the workbench database."""

    def __init__(self, database_path: str):
        self.path = os.path.abspath(database_path) + ".lock"
        self._handle = None

    def __enter__(self) -> "FileLock":
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._handle = open(self.path, "a+b")
        self._handle.seek(0)
        if self._handle.read(1) == b"":
            self._handle.seek(0)
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            self._handle.close()
            self._handle = None
            fail(E_DB_BUSY, "Unable to acquire workbench file lock", path=self.path, detail=str(exc))
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


def require_active_snapshot(connection: sqlite3.Connection) -> str:
    snapshot_version = get_meta(connection, "active_snapshot_version")
    if not snapshot_version:
        fail(E_NOT_LOADED, "No active tracking snapshot")
    return snapshot_version


def make_workbench_error(exc: sqlite3.Error) -> WorkbenchError:
    text = str(exc).lower()
    code = E_DB_BUSY if "locked" in text or "busy" in text else E_STATE_CORRUPT
    return WorkbenchError(code=code, message="SQLite operation failed", context={"detail": str(exc)})

