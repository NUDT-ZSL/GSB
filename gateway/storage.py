from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import E_NOT_LOADED, E_STORAGE, GatewayError

try:  # pragma: no cover - platform selection
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None

try:  # pragma: no cover - platform selection
    import msvcrt
except ImportError:  # pragma: no cover
    msvcrt = None


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    name TEXT PRIMARY KEY,
    partition_count INTEGER NOT NULL,
    partition_key_rule TEXT NOT NULL,
    backlog_limit INTEGER NOT NULL,
    out_of_order_tolerance_window INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS subscriptions (
    subscriber_id TEXT PRIMARY KEY,
    topic TEXT NOT NULL REFERENCES topics(name),
    pull_batch_size INTEGER NOT NULL,
    consume_rate_per_sec REAL NOT NULL,
    failure_rate REAL NOT NULL,
    retry_budget INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS gateway_config (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS partition_log (
    partition_id TEXT NOT NULL,
    offset_no INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    event_id TEXT NOT NULL UNIQUE,
    topic TEXT NOT NULL,
    partition_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    expected_delivery_generation INTEGER NOT NULL,
    PRIMARY KEY (partition_id, offset_no),
    UNIQUE (topic, event_seq)
);

CREATE TABLE IF NOT EXISTS ledger_versions (
    version INTEGER PRIMARY KEY,
    snapshot TEXT NOT NULL,
    checksum TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL REFERENCES ledger_versions(version)
);

CREATE TABLE IF NOT EXISTS rounds (
    round_id TEXT PRIMARY KEY,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS round_partitions (
    round_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    planned_batch INTEGER NOT NULL,
    completed_attempts INTEGER NOT NULL DEFAULT 0,
    estimated_cost_ms INTEGER NOT NULL DEFAULT 0,
    actual_cost_ms INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    reason TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (round_id, partition_id)
);

CREATE TABLE IF NOT EXISTS round_state (
    round_id TEXT PRIMARY KEY,
    subscriber_id TEXT NOT NULL,
    budget_ms INTEGER NOT NULL,
    budget_remaining_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL,
    partition_id TEXT NOT NULL,
    subscriber_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    event_offset INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    status TEXT NOT NULL,
    reserved_clock_ms INTEGER NOT NULL,
    completed_clock_ms INTEGER,
    failure_reason TEXT,
    owner_pid INTEGER NOT NULL,
    UNIQUE (subscriber_id, partition_id, event_offset, generation)
);

CREATE TABLE IF NOT EXISTS subscriber_inbox (
    subscriber_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    first_generation INTEGER NOT NULL,
    last_generation INTEGER NOT NULL,
    actual_count INTEGER NOT NULL,
    duplicate_count INTEGER NOT NULL,
    first_delivery_id TEXT NOT NULL,
    last_delivery_id TEXT NOT NULL,
    PRIMARY KEY (subscriber_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_partition_log_order
    ON partition_log(partition_id, offset_no);
CREATE INDEX IF NOT EXISTS idx_deliveries_partition
    ON deliveries(partition_id, subscriber_id, event_offset);
CREATE INDEX IF NOT EXISTS idx_rounds_created
    ON rounds(created_at);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def snapshot_checksum(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode("utf-8")).hexdigest()


class FileLock:
    """Portable advisory file lock used for partition/round critical sections."""

    def __init__(self, path: str | Path, timeout: float = 30.0):
        self.path = Path(path)
        self.timeout = timeout
        self._handle: Any = None
        self._locked = False

    def _try_lock(self) -> bool:
        if fcntl is not None:
            try:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        if msvcrt is not None:  # pragma: no cover - Windows path
            try:
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        raise GatewayError(
            E_STORAGE,
            "no supported file-locking mechanism is available",
            {"path": str(self.path)},
        )

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+b")
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            if self._try_lock():
                self._locked = True
                return self
            if time.monotonic() >= deadline:
                self._handle.close()
                self._handle = None
                raise GatewayError(
                    E_STORAGE,
                    "timed out while waiting for a file lock",
                    {"path": str(self.path), "timeout_seconds": self.timeout},
                )
            time.sleep(0.01)

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._handle is None:
            return
        if self._locked:
            if fcntl is not None:
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows path
                try:
                    self._handle.seek(0)
                    msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
        self._handle.close()
        self._handle = None
        self._locked = False


class Storage:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.lock_root = self.db_path.with_name(self.db_path.name + ".locks")

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(SCHEMA_SQL)

    @contextmanager
    def transaction(self, connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        else:
            connection.execute("COMMIT")

    def load_state(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute(
            """
            SELECT p.version, v.snapshot, v.checksum
            FROM ledger_pointer p
            JOIN ledger_versions v ON v.version = p.version
            WHERE p.singleton = 1
            """
        ).fetchone()
        if row is None:
            return None
        expected = snapshot_checksum(row["snapshot"])
        if expected != row["checksum"]:
            raise GatewayError(
                E_STORAGE,
                "ledger snapshot checksum mismatch",
                {
                    "version": row["version"],
                    "expected_checksum": expected,
                    "stored_checksum": row["checksum"],
                },
            )
        state = json.loads(row["snapshot"])
        if state.get("state_version") != row["version"]:
            raise GatewayError(
                E_STORAGE,
                "ledger version pointer is inconsistent with the snapshot",
                {
                    "pointer_version": row["version"],
                    "snapshot_version": state.get("state_version"),
                },
            )
        return state

    def require_state(self, connection: sqlite3.Connection) -> dict[str, Any]:
        state = self.load_state(connection)
        if state is None:
            raise GatewayError(
                E_NOT_LOADED,
                "gateway database has not been loaded",
                {"db_path": str(self.db_path)},
            )
        return state

    def commit_state(
        self,
        connection: sqlite3.Connection,
        state: dict[str, Any],
    ) -> int:
        row = connection.execute(
            "SELECT version FROM ledger_pointer WHERE singleton = 1"
        ).fetchone()
        next_version = (int(row["version"]) if row else 0) + 1
        state["state_version"] = next_version
        snapshot = canonical_json(state)
        checksum = snapshot_checksum(snapshot)
        connection.execute(
            """
            INSERT INTO ledger_versions(version, snapshot, checksum, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                next_version,
                snapshot,
                checksum,
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            ),
        )
        connection.execute(
            """
            INSERT INTO ledger_pointer(singleton, version) VALUES (1, ?)
            ON CONFLICT(singleton) DO UPDATE SET version = excluded.version
            """,
            (next_version,),
        )
        return next_version

    def read_topics(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT name, partition_count, partition_key_rule,
                   backlog_limit, out_of_order_tolerance_window
            FROM topics ORDER BY name
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def read_subscriptions(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            """
            SELECT subscriber_id, topic, pull_batch_size, consume_rate_per_sec,
                   failure_rate, retry_budget
            FROM subscriptions ORDER BY subscriber_id
            """
        ).fetchall()
        return [dict(row) for row in rows]

    def read_config(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT payload FROM gateway_config WHERE singleton = 1"
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def read_meta(self, connection: sqlite3.Connection) -> dict[str, str]:
        rows = connection.execute("SELECT key, value FROM meta ORDER BY key").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def write_metadata(
        self,
        connection: sqlite3.Connection,
        *,
        topics: list[dict[str, Any]],
        subscriptions: list[dict[str, Any]],
        config: dict[str, Any],
        metadata_hash: str,
        sample_hash: str,
    ) -> None:
        connection.executemany(
            """
            INSERT INTO topics(
                name, partition_count, partition_key_rule,
                backlog_limit, out_of_order_tolerance_window
            ) VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    topic["name"],
                    topic["partition_count"],
                    topic["partition_key_rule"],
                    topic["backlog_limit"],
                    topic["out_of_order_tolerance_window"],
                )
                for topic in topics
            ],
        )
        connection.executemany(
            """
            INSERT INTO subscriptions(
                subscriber_id, topic, pull_batch_size, consume_rate_per_sec,
                failure_rate, retry_budget
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    sub["subscriber_id"],
                    sub["topic"],
                    sub["pull_batch_size"],
                    sub["consume_rate_per_sec"],
                    sub["failure_rate"],
                    sub["retry_budget"],
                )
                for sub in subscriptions
            ],
        )
        connection.execute(
            "INSERT INTO gateway_config(singleton, payload) VALUES (1, ?)",
            (canonical_json(config),),
        )
        connection.executemany(
            "INSERT INTO meta(key, value) VALUES (?, ?)",
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("metadata_hash", metadata_hash),
                ("sample_hash", sample_hash),
            ],
        )

    def partition_lock(self, partition_key: str, timeout: float = 30.0) -> FileLock:
        digest = hashlib.sha256(partition_key.encode("utf-8")).hexdigest()[:24]
        safe = "".join(
            ch if ch.isalnum() or ch in "-_." else "_"
            for ch in partition_key
        )[:64]
        return FileLock(self.lock_root / f"partition-{safe}-{digest}.lock", timeout)

    def round_lock(self, round_id: str, timeout: float = 30.0) -> FileLock:
        digest = hashlib.sha256(round_id.encode("utf-8")).hexdigest()[:24]
        return FileLock(self.lock_root / f"round-{digest}.lock", timeout)

    def db_fingerprint(self) -> str:
        try:
            stat = self.db_path.stat()
        except OSError:
            return "missing"
        token = f"{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        return hashlib.sha256(token).hexdigest()

    @staticmethod
    def pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        if pid == os.getpid():
            return True
        if os.name == "nt":  # pragma: no cover - Windows path
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(
                process_query_limited_information,
                False,
                pid,
            )
            if handle:
                kernel32.CloseHandle(handle)
                return True
            error = ctypes.get_last_error()
            return error == 5
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True
