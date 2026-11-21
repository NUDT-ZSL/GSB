from __future__ import annotations

import fcntl
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import BusinessError, ErrorCode

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS buckets (
    bucket TEXT PRIMARY KEY,
    config_json TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    load_digest TEXT NOT NULL,
    clock_watermark INTEGER NOT NULL CHECK(clock_watermark >= 0),
    clock_tolerance INTEGER NOT NULL CHECK(clock_tolerance >= 0),
    round_watermark INTEGER NOT NULL CHECK(round_watermark >= 0),
    mode_last TEXT,
    decision_json TEXT
);

CREATE TABLE IF NOT EXISTS layers (
    bucket TEXT NOT NULL,
    layer TEXT NOT NULL CHECK(layer IN ('hot', 'cold')),
    capacity_bytes INTEGER NOT NULL CHECK(capacity_bytes > 0),
    used_bytes INTEGER NOT NULL CHECK(used_bytes >= 0),
    PRIMARY KEY(bucket, layer),
    FOREIGN KEY(bucket) REFERENCES buckets(bucket) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS objects (
    bucket TEXT NOT NULL,
    key TEXT NOT NULL,
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    last_access_clock INTEGER NOT NULL CHECK(last_access_clock >= 0),
    access_count INTEGER NOT NULL CHECK(access_count >= 0),
    heat_weight TEXT NOT NULL,
    declared_layer TEXT NOT NULL CHECK(declared_layer IN ('hot', 'cold')),
    current_layer TEXT NOT NULL CHECK(current_layer IN ('hot', 'cold')),
    migratable INTEGER NOT NULL CHECK(migratable IN (0, 1)),
    migration_state TEXT NOT NULL CHECK(
        migration_state IN (
            'pending', 'migrating', 'migrated', 'failed', 'abandoned'
        )
    ),
    failure_count INTEGER NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    backoff_until_clock INTEGER NOT NULL DEFAULT 0 CHECK(backoff_until_clock >= 0),
    version INTEGER NOT NULL DEFAULT 0 CHECK(version >= 0),
    last_round INTEGER NOT NULL DEFAULT 0 CHECK(last_round >= 0),
    last_outcome TEXT,
    PRIMARY KEY(bucket, key),
    FOREIGN KEY(bucket) REFERENCES buckets(bucket) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS batches (
    bucket TEXT NOT NULL,
    round INTEGER NOT NULL CHECK(round > 0),
    request_clock INTEGER NOT NULL CHECK(request_clock >= 0),
    effective_clock INTEGER NOT NULL CHECK(effective_clock >= 0),
    request_digest TEXT NOT NULL,
    mode TEXT NOT NULL CHECK(mode IN ('exact', 'sampled')),
    status TEXT NOT NULL CHECK(status IN ('in_flight', 'completed')),
    decision_json TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    result_json TEXT,
    PRIMARY KEY(bucket, round),
    FOREIGN KEY(bucket) REFERENCES buckets(bucket) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS batch_items (
    bucket TEXT NOT NULL,
    round INTEGER NOT NULL,
    key TEXT NOT NULL,
    object_version INTEGER NOT NULL CHECK(object_version >= 0),
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    source_layer TEXT NOT NULL CHECK(source_layer IN ('hot', 'cold')),
    target_layer TEXT NOT NULL CHECK(target_layer IN ('hot', 'cold')),
    score TEXT NOT NULL,
    order_index INTEGER NOT NULL CHECK(order_index >= 1),
    start_order INTEGER,
    planned_outcome TEXT NOT NULL CHECK(
        planned_outcome IN (
            'success', 'failure', 'not_started', 'abandoned'
        )
    ),
    state TEXT NOT NULL CHECK(
        state IN (
            'reserved', 'not_started', 'abandoned', 'committed',
            'released', 'conflict'
        )
    ),
    outcome TEXT,
    reason TEXT,
    error_code TEXT,
    PRIMARY KEY(bucket, round, key),
    FOREIGN KEY(bucket, round) REFERENCES batches(bucket, round) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS inflight (
    bucket TEXT NOT NULL,
    round INTEGER NOT NULL,
    key TEXT NOT NULL,
    object_version INTEGER NOT NULL CHECK(object_version >= 0),
    source_layer TEXT NOT NULL CHECK(source_layer IN ('hot', 'cold')),
    target_layer TEXT NOT NULL CHECK(target_layer IN ('hot', 'cold')),
    size_bytes INTEGER NOT NULL CHECK(size_bytes > 0),
    status TEXT NOT NULL CHECK(
        status IN ('reserved', 'committed', 'released', 'conflict')
    ),
    PRIMARY KEY(bucket, round, key, object_version),
    FOREIGN KEY(bucket, key) REFERENCES objects(bucket, key) ON DELETE CASCADE,
    FOREIGN KEY(bucket, round) REFERENCES batches(bucket, round) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_objects_bucket_layer
    ON objects(bucket, current_layer, key);
CREATE INDEX IF NOT EXISTS ix_batches_status
    ON batches(bucket, status, round);
CREATE INDEX IF NOT EXISTS ix_inflight_status
    ON inflight(bucket, status, target_layer);
CREATE UNIQUE INDEX IF NOT EXISTS ux_inflight_active_object
    ON inflight(bucket, key) WHERE status = 'reserved';
"""


def database_exists(path: str | Path) -> bool:
    return Path(path).is_file()


@contextmanager
def file_lock(path: str | Path, *, exclusive: bool = True) -> Iterator[None]:
    lock_path = Path(f"{path}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(descriptor, operation)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def connect_database(
    path: str | Path,
    *,
    create: bool = False,
) -> sqlite3.Connection:
    source = Path(path)
    existing_nonempty = source.is_file() and source.stat().st_size > 0
    if not create and not source.is_file():
        raise BusinessError(
            ErrorCode.DB_NOT_FOUND,
            "workspace database does not exist",
            {"path": str(source)},
        )
    if create:
        source.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        str(source),
        timeout=30.0,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    if create and not existing_nonempty:
        init_schema(connection)
    else:
        ensure_schema(connection)
    return connection


def init_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.execute(
        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )


def ensure_schema(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.DatabaseError as exc:
        raise BusinessError(
            ErrorCode.DB_SCHEMA_INVALID,
            "workspace database is not initialized by this workbench",
        ) from exc
    if row is None or int(row["value"]) != SCHEMA_VERSION:
        raise BusinessError(
            ErrorCode.DB_SCHEMA_INVALID,
            "unsupported workspace database schema version",
            {
                "found": None if row is None else row["value"],
                "expected": SCHEMA_VERSION,
            },
        )


@contextmanager
def transaction(
    connection: sqlite3.Connection,
    *,
    immediate: bool = True,
) -> Iterator[sqlite3.Connection]:
    statement = "BEGIN IMMEDIATE" if immediate else "BEGIN"
    try:
        connection.execute(statement)
    except sqlite3.OperationalError as exc:
        raise BusinessError(
            ErrorCode.INTERNAL,
            "could not acquire SQLite write transaction",
            {"error": str(exc)},
        ) from exc
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def layer_used(connection: sqlite3.Connection, bucket: str) -> dict[str, int]:
    rows = connection.execute(
        "SELECT layer, used_bytes FROM layers WHERE bucket = ?",
        (bucket,),
    ).fetchall()
    return {row["layer"]: int(row["used_bytes"]) for row in rows}


def layer_capacity(connection: sqlite3.Connection, bucket: str) -> dict[str, int]:
    rows = connection.execute(
        "SELECT layer, capacity_bytes FROM layers WHERE bucket = ?",
        (bucket,),
    ).fetchall()
    return {row["layer"]: int(row["capacity_bytes"]) for row in rows}


def layer_reserved(connection: sqlite3.Connection, bucket: str) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT target_layer, COALESCE(SUM(size_bytes), 0) AS reserved
        FROM inflight
        WHERE bucket = ? AND status = 'reserved'
        GROUP BY target_layer
        """,
        (bucket,),
    ).fetchall()
    result = {"hot": 0, "cold": 0}
    for row in rows:
        result[row["target_layer"]] = int(row["reserved"])
    return result


def reconcile_ledger(connection: sqlite3.Connection, bucket: str) -> dict[str, int]:
    computed_rows = connection.execute(
        """
        SELECT current_layer, COALESCE(SUM(size_bytes), 0) AS total
        FROM objects
        WHERE bucket = ?
        GROUP BY current_layer
        """,
        (bucket,),
    ).fetchall()
    computed = {"hot": 0, "cold": 0}
    for row in computed_rows:
        computed[row["current_layer"]] = int(row["total"])
    for layer in ("hot", "cold"):
        connection.execute(
            "UPDATE layers SET used_bytes = ? WHERE bucket = ? AND layer = ?",
            (computed[layer], bucket, layer),
        )
    return computed
