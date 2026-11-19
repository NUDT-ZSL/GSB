"""SQLite persistence primitives and deterministic digest calculations."""

from __future__ import annotations

import fcntl
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import E_DATABASE, EngineError
from .model import canonical_json, digest_text

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    watermark_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    name TEXT PRIMARY KEY,
    partition_count INTEGER NOT NULL,
    capacities_json TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    topic_json TEXT NOT NULL,
    digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partition_state (
    topic TEXT NOT NULL,
    partition INTEGER NOT NULL,
    next_offset INTEGER NOT NULL,
    append_digest TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    PRIMARY KEY (topic, partition)
);

CREATE TABLE IF NOT EXISTS routing_keys (
    topic TEXT NOT NULL,
    ordering_key TEXT NOT NULL,
    partition INTEGER NOT NULL,
    PRIMARY KEY (topic, ordering_key)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_seq INTEGER PRIMARY KEY,
    topic TEXT NOT NULL,
    ordering_key TEXT NOT NULL,
    tx_id TEXT,
    logical_ts_ms INTEGER NOT NULL,
    canonical_json TEXT NOT NULL,
    batch_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    partition INTEGER,
    message_count INTEGER NOT NULL,
    first_offset INTEGER
);

CREATE INDEX IF NOT EXISTS batches_topic_status
    ON batches (topic, status, batch_seq);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    prepared_at_ms INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    timeout_ms INTEGER NOT NULL,
    committed_at_ms INTEGER,
    finalized_at_ms INTEGER,
    tx_digest TEXT NOT NULL,
    diagnostic_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS transactions_status_deadline
    ON transactions (status, deadline_ms, tx_id);

CREATE TABLE IF NOT EXISTS partition_log (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    partition INTEGER NOT NULL,
    "offset" INTEGER NOT NULL,
    batch_seq INTEGER NOT NULL,
    message_index INTEGER NOT NULL,
    message_id TEXT NOT NULL,
    ordering_key TEXT NOT NULL,
    payload_encoding TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    logical_ts_ms INTEGER NOT NULL,
    tx_id TEXT,
    visibility TEXT NOT NULL,
    append_digest TEXT NOT NULL,
    UNIQUE (topic, partition, "offset"),
    UNIQUE (topic, batch_seq, message_index)
);

CREATE INDEX IF NOT EXISTS partition_log_scan
    ON partition_log (topic, partition, "offset");

CREATE INDEX IF NOT EXISTS partition_log_tx
    ON partition_log (tx_id, visibility);

CREATE TABLE IF NOT EXISTS consumer_offsets (
    consumer_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    partition INTEGER NOT NULL,
    committed_offset INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (consumer_id, topic, partition)
);

CREATE TABLE IF NOT EXISTS deliveries (
    consumer_id TEXT NOT NULL,
    topic TEXT NOT NULL,
    partition INTEGER NOT NULL,
    "offset" INTEGER NOT NULL,
    delivery_id TEXT NOT NULL UNIQUE,
    batch_seq INTEGER NOT NULL,
    message_index INTEGER NOT NULL,
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    delivered_clock_ms INTEGER NOT NULL,
    acked_clock_ms INTEGER,
    PRIMARY KEY (consumer_id, topic, partition, "offset")
);

CREATE INDEX IF NOT EXISTS deliveries_pending
    ON deliveries (consumer_id, topic, partition, status, "offset");

CREATE TABLE IF NOT EXISTS operations (
    op_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    op_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    topic TEXT,
    partition INTEGER,
    batch_seq INTEGER,
    tx_id TEXT,
    consumer_id TEXT,
    "offset" INTEGER,
    expected_digest TEXT,
    requested_clock_ms INTEGER,
    created_clock_ms INTEGER NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    diagnostic_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS operations_pending
    ON operations (status, op_seq);

CREATE TABLE IF NOT EXISTS injections (
    injection_id TEXT PRIMARY KEY,
    stage TEXT NOT NULL,
    action TEXT NOT NULL,
    timing TEXT NOT NULL,
    error_code TEXT,
    occurrence INTEGER NOT NULL,
    fired_count INTEGER NOT NULL DEFAULT 0,
    match_json TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    last_clock_ms INTEGER
);
"""

GENESIS_SUFFIX = "v1"


class DatabaseLock:
    """Advisory process lock used around compound SQLite transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self.lock_path = Path(str(db_path) + ".lock")
        self._handle = None

    def __enter__(self) -> "DatabaseLock":
        self._handle = self.lock_path.open("a+b")
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def genesis_append_digest(topic: str, partition: int) -> str:
    return digest_text(f"append:{GENESIS_SUFFIX}:{topic}:{partition}")


def next_append_digest(previous_digest: str, record: dict[str, Any]) -> str:
    immutable = {
        "batch_seq": record["batch_seq"],
        "logical_ts_ms": record["logical_ts_ms"],
        "message_id": record["message_id"],
        "message_index": record["message_index"],
        "offset": record["offset"],
        "ordering_key": record["ordering_key"],
        "partition": record["partition"],
        "payload_digest": record["payload_digest"],
        "payload_encoding": record["payload_encoding"],
        "topic": record["topic"],
        "tx_id": record["tx_id"],
    }
    return digest_text(previous_digest + "\n" + canonical_json(immutable))


def compute_state_digest(conn: sqlite3.Connection, topic: str, partition: int) -> str:
    offset_row = conn.execute(
        'SELECT next_offset FROM partition_state WHERE topic = ? AND partition = ?',
        (topic, partition),
    ).fetchone()
    next_offset = int(offset_row["next_offset"])
    rows = conn.execute(
        """
        SELECT "offset", append_digest, visibility
        FROM partition_log
        WHERE topic = ? AND partition = ?
        ORDER BY "offset"
        """,
        (topic, partition),
    ).fetchall()
    material = {
        "next_offset": next_offset,
        "partition": partition,
        "records": [
            {
                "append_digest": row["append_digest"],
                "offset": row["offset"],
                "visibility": row["visibility"],
            }
            for row in rows
        ],
        "topic": topic,
    }
    return digest_text(canonical_json(material))


def refresh_state_digest(conn: sqlite3.Connection, topic: str, partition: int) -> None:
    state_digest = compute_state_digest(conn, topic, partition)
    conn.execute(
        "UPDATE partition_state SET state_digest = ? WHERE topic = ? AND partition = ?",
        (state_digest, topic, partition),
    )


def refresh_all_state_digests(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT topic, partition FROM partition_state ORDER BY topic, partition").fetchall()
    for row in rows:
        refresh_state_digest(conn, row["topic"], row["partition"])


class Store:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        try:
            conn = sqlite3.connect(
                str(self.db_path),
                timeout=5.0,
                isolation_level=None,
            )
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute("PRAGMA synchronous = FULL")
            return conn
        except sqlite3.Error as exc:
            raise EngineError(
                E_DATABASE,
                "cannot open SQLite database",
                details={"database": str(self.db_path), "reason": str(exc)},
                exit_code=3,
            ) from exc

    def open(self, *, create: bool) -> "Store":
        if not create and not self.db_path.exists():
            raise EngineError(
                E_DATABASE,
                "database does not exist; run load first",
                details={"database": str(self.db_path)},
                exit_code=3,
            )
        if create:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            if create:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(SCHEMA)
                row = conn.execute("PRAGMA user_version").fetchone()
                version = int(row[0])
                if version not in (0, SCHEMA_VERSION):
                    raise EngineError(
                        E_DATABASE,
                        "database schema version is unsupported",
                        details={"found": version, "supported": SCHEMA_VERSION},
                        exit_code=3,
                    )
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            else:
                row = conn.execute("PRAGMA user_version").fetchone()
                if int(row[0]) != SCHEMA_VERSION:
                    raise EngineError(
                        E_DATABASE,
                        "database is not initialized by this engine",
                        details={"database": str(self.db_path)},
                        exit_code=3,
                    )
                conn.execute("PRAGMA journal_mode = WAL")
        except EngineError:
            conn.close()
            raise
        except sqlite3.Error as exc:
            conn.close()
            raise EngineError(
                E_DATABASE,
                "cannot initialize SQLite database",
                details={"database": str(self.db_path), "reason": str(exc)},
                exit_code=3,
            ) from exc
        self.conn = conn
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        assert self.conn is not None
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            try:
                self.conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def meta_get(self, key: str) -> str | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def meta_set(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute(
            """
            INSERT INTO meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def read_clock(self) -> int:
        assert self.conn is not None
        row = self.conn.execute("SELECT watermark_ms FROM clock_state WHERE singleton = 1").fetchone()
        if row is None:
            raise EngineError(
                E_DATABASE,
                "database has no logical clock state",
                exit_code=3,
            )
        return int(row["watermark_ms"])

    def advance_clock(self, conn: sqlite3.Connection, logical_ts_ms: int) -> int:
        row = conn.execute("SELECT watermark_ms FROM clock_state WHERE singleton = 1").fetchone()
        current = int(row["watermark_ms"])
        if logical_ts_ms < current:
            raise EngineError(
                "E_CLOCK_REGRESSION",
                "logical clock cannot move backwards",
                details={
                    "current_watermark_ms": current,
                    "requested_ms": logical_ts_ms,
                },
            )
        if logical_ts_ms > current:
            conn.execute(
                "UPDATE clock_state SET watermark_ms = ? WHERE singleton = 1",
                (logical_ts_ms,),
            )
        return max(current, logical_ts_ms)

    def initialize_partition(self, conn: sqlite3.Connection, topic: str, partition: int) -> None:
        state_digest = digest_text(canonical_json({"next_offset": 0, "partition": partition, "records": [], "topic": topic}))
        conn.execute(
            """
            INSERT OR IGNORE INTO partition_state
                (topic, partition, next_offset, append_digest, state_digest)
            VALUES (?, ?, 0, ?, ?)
            """,
            (topic, partition, genesis_append_digest(topic, partition), state_digest),
        )

