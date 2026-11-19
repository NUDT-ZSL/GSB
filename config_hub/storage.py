"""SQLite persistence for configuration versions, runs, and results."""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, strict_json_loads
from .locking import file_lock

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_version INTEGER NOT NULL,
    logical_watermark_ms INTEGER NOT NULL CHECK (logical_watermark_ms >= 0),
    clock_rollback_tolerance_ms INTEGER NOT NULL
        CHECK (clock_rollback_tolerance_ms >= 0),
    FOREIGN KEY (current_version) REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS config_versions (
    version INTEGER PRIMARY KEY CHECK (version >= 1),
    parent_version INTEGER,
    config_digest TEXT NOT NULL UNIQUE,
    effective_clock_ms INTEGER NOT NULL CHECK (effective_clock_ms >= 0),
    applied_clock_ms INTEGER NOT NULL CHECK (applied_clock_ms >= 0),
    patch_id TEXT,
    snapshot_json TEXT NOT NULL,
    FOREIGN KEY (parent_version) REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    target_version INTEGER NOT NULL CHECK (target_version >= 2),
    base_version INTEGER NOT NULL CHECK (base_version >= 1),
    content_digest TEXT NOT NULL,
    effective_from_ms INTEGER NOT NULL CHECK (effective_from_ms >= 0),
    effective_until_ms INTEGER NOT NULL CHECK (effective_until_ms >= 0),
    requested_clock_ms INTEGER NOT NULL CHECK (requested_clock_ms >= 0),
    status TEXT NOT NULL CHECK (status IN ('prepared', 'applied', 'aborted')),
    applied_version INTEGER,
    applied_config_digest TEXT,
    error_code TEXT,
    patch_json TEXT NOT NULL,
    CHECK (effective_from_ms <= effective_until_ms)
);

CREATE UNIQUE INDEX IF NOT EXISTS patches_active_target
    ON patches(target_version)
    WHERE status IN ('prepared', 'applied');

CREATE TABLE IF NOT EXISTS compute_runs (
    client_id TEXT NOT NULL,
    round_no INTEGER NOT NULL CHECK (round_no >= 1),
    status TEXT NOT NULL CHECK (status IN ('in_progress', 'completed')),
    config_version INTEGER NOT NULL,
    config_digest TEXT NOT NULL,
    input_json TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    started_clock_ms INTEGER NOT NULL CHECK (started_clock_ms >= 0),
    result_digest TEXT,
    PRIMARY KEY (client_id, round_no),
    FOREIGN KEY (config_version) REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS results (
    client_id TEXT NOT NULL,
    round_no INTEGER NOT NULL CHECK (round_no >= 1),
    config_version INTEGER NOT NULL,
    config_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    result_json TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    committed_clock_ms INTEGER NOT NULL CHECK (committed_clock_ms >= 0),
    PRIMARY KEY (client_id, round_no),
    FOREIGN KEY (client_id, round_no)
        REFERENCES compute_runs(client_id, round_no),
    FOREIGN KEY (config_version) REFERENCES config_versions(version)
);

CREATE TABLE IF NOT EXISTS result_dependencies (
    client_id TEXT NOT NULL,
    round_no INTEGER NOT NULL,
    config_version INTEGER NOT NULL,
    config_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    PRIMARY KEY (client_id, round_no),
    FOREIGN KEY (client_id, round_no)
        REFERENCES results(client_id, round_no),
    FOREIGN KEY (config_version) REFERENCES config_versions(version)
);

CREATE INDEX IF NOT EXISTS result_dependencies_by_version
    ON result_dependencies(config_version, client_id, round_no);

CREATE TABLE IF NOT EXISTS client_state (
    client_id TEXT PRIMARY KEY,
    last_round_no INTEGER NOT NULL,
    bound_version INTEGER NOT NULL,
    bound_digest TEXT NOT NULL,
    result_digest TEXT NOT NULL,
    updated_clock_ms INTEGER NOT NULL,
    FOREIGN KEY (bound_version) REFERENCES config_versions(version)
);
"""


class Store:
    def __init__(self, db_path: str | os.PathLike[str]) -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = Path(str(self.db_path) + ".lock")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(SCHEMA)
        return connection

    @contextlib.contextmanager
    def transaction(
        self, *, immediate: bool = False
    ) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    @contextlib.contextmanager
    def write_lock(self) -> Iterator[None]:
        with file_lock(self.lock_path):
            yield

    @staticmethod
    def get_state(connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM state WHERE id = 1").fetchone()

    @staticmethod
    def get_version_row(
        connection: sqlite3.Connection, version: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT * FROM config_versions WHERE version = ?", (version,)
        ).fetchone()

    @staticmethod
    def parse_snapshot_row(row: sqlite3.Row) -> dict[str, Any]:
        return strict_json_loads(row["snapshot_json"])

    def current_snapshot(
        self, connection: sqlite3.Connection
    ) -> tuple[sqlite3.Row, dict[str, Any]]:
        state = self.get_state(connection)
        if state is None:
            raise RuntimeError("configuration store is not initialized")
        version_row = self.get_version_row(connection, state["current_version"])
        if version_row is None:
            raise RuntimeError("current configuration version is missing")
        return state, self.parse_snapshot_row(version_row)

    @staticmethod
    def version_snapshot(
        connection: sqlite3.Connection, version: int
    ) -> dict[str, Any] | None:
        row = Store.get_version_row(connection, version)
        if row is None:
            return None
        return Store.parse_snapshot_row(row)

    @staticmethod
    def snapshot_json(snapshot: dict[str, Any]) -> str:
        return canonical_json(snapshot)
