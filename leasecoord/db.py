from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .config import ClusterConfig, parse_cluster
from .errors import fail
from .numeric import fraction_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_config (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    config_json TEXT NOT NULL,
    config_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nodes (
    name TEXT PRIMARY KEY,
    priority INTEGER NOT NULL,
    lease_ms INTEGER NOT NULL,
    heartbeat_ms INTEGER NOT NULL,
    session_epoch INTEGER NOT NULL,
    data_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS session_watermarks (
    name TEXT PRIMARY KEY,
    max_session_epoch INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    clock_text TEXT NOT NULL,
    clock_num TEXT NOT NULL,
    clock_den TEXT NOT NULL,
    clock_units INTEGER NOT NULL,
    clock_residual_num TEXT NOT NULL,
    clock_residual_den TEXT NOT NULL,
    leader TEXT,
    election_id TEXT,
    config_hash TEXT NOT NULL,
    epoch_counter INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS leases (
    node_name TEXT PRIMARY KEY REFERENCES nodes(name),
    lease_generation INTEGER NOT NULL,
    session_epoch INTEGER NOT NULL,
    data_version INTEGER NOT NULL,
    granted_text TEXT NOT NULL,
    granted_num TEXT NOT NULL,
    granted_den TEXT NOT NULL,
    expires_text TEXT NOT NULL,
    expires_num TEXT NOT NULL,
    expires_den TEXT NOT NULL,
    status TEXT NOT NULL,
    remaining_units INTEGER NOT NULL,
    remaining_residual_num TEXT NOT NULL,
    remaining_residual_den TEXT NOT NULL,
    safety_units INTEGER NOT NULL,
    safety_residual_num TEXT NOT NULL,
    safety_residual_den TEXT NOT NULL,
    last_renew_key TEXT
);

CREATE TABLE IF NOT EXISTS lease_history (
    node_name TEXT NOT NULL REFERENCES nodes(name),
    lease_generation INTEGER NOT NULL,
    session_epoch INTEGER NOT NULL,
    data_version INTEGER NOT NULL,
    granted_text TEXT NOT NULL,
    expires_text TEXT NOT NULL,
    request_key TEXT NOT NULL UNIQUE,
    PRIMARY KEY (node_name, lease_generation)
);

CREATE TABLE IF NOT EXISTS renewals (
    request_key TEXT PRIMARY KEY,
    node_name TEXT NOT NULL,
    lease_generation INTEGER NOT NULL,
    session_epoch INTEGER NOT NULL,
    data_version INTEGER NOT NULL,
    clock_text TEXT NOT NULL,
    expires_text TEXT NOT NULL,
    response_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS elections (
    election_id TEXT PRIMARY KEY,
    election_key TEXT NOT NULL UNIQUE,
    op_id TEXT NOT NULL UNIQUE,
    clock_text TEXT NOT NULL,
    leader TEXT,
    state_json TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    response_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precision_records (
    election_id TEXT NOT NULL REFERENCES elections(election_id),
    pair_key TEXT NOT NULL,
    node_a TEXT NOT NULL,
    node_b TEXT NOT NULL,
    score_a_units INTEGER NOT NULL,
    score_b_units INTEGER NOT NULL,
    gap_ms TEXT NOT NULL,
    tolerance_ms TEXT NOT NULL,
    error_source_chain_json TEXT NOT NULL,
    affected_candidates_json TEXT NOT NULL,
    first_rejection_json TEXT NOT NULL,
    PRIMARY KEY (election_id, pair_key)
);

CREATE TABLE IF NOT EXISTS prepared_ops (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    request_key TEXT NOT NULL,
    clock_text TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    response_json TEXT,
    clean_reason TEXT
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    clock_text TEXT NOT NULL,
    node_name TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_leases_status ON leases(status);
CREATE INDEX IF NOT EXISTS idx_renewals_node_clock ON renewals(node_name, clock_text);
CREATE INDEX IF NOT EXISTS idx_prepared_status ON prepared_ops(status);
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def answer_json(value: Any, *, compact: bool = False) -> str:
    if compact:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False)


class Database:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.db_path = workspace / "coord.db"
        self.lock_path = workspace / ".coord.lock"

    def exists(self) -> bool:
        return self.db_path.exists()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def initialize(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        connection = self.connect()
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                fail("E_WAL_UNAVAILABLE", "SQLite did not enable WAL mode", details={"mode": str(mode)})
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
            )
        finally:
            connection.close()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path, "a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if handle.read(1) == b"":
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

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
        finally:
            connection.close()

    def require_loaded(self) -> None:
        if not self.exists():
            fail(
                "E_NOT_LOADED",
                "Workspace has not been loaded",
                details={"workspace": str(self.workspace)},
            )
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='cluster_config'"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            fail("E_NOT_LOADED", "Workspace schema is not initialized")

    def read_cluster(self, connection: sqlite3.Connection) -> ClusterConfig:
        row = connection.execute(
            "SELECT config_json, config_hash FROM cluster_config WHERE singleton = 1"
        ).fetchone()
        if row is None:
            fail("E_NOT_LOADED", "Cluster configuration is missing")
        try:
            document = json.loads(row["config_json"])
            return parse_cluster(document["nodes"], document["election"])
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            fail(
                "E_STATE_CORRUPT",
                "Persisted cluster configuration is invalid",
                details={"reason": str(exc)},
            )
            raise AssertionError("unreachable")

    def event(
        self,
        connection: sqlite3.Connection,
        *,
        kind: str,
        clock_text: str,
        node_name: str | None,
        code: str,
        message: str,
        details: dict[str, Any],
    ) -> str:
        event_id = "evt_" + stable_hash(
            {
                "kind": kind,
                "clock": clock_text,
                "node": node_name,
                "code": code,
                "details": details,
            }
        )[:24]
        connection.execute(
            """
            INSERT OR IGNORE INTO audit_events(
                event_id, kind, clock_text, node_name, code, message, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                kind,
                clock_text,
                node_name,
                code,
                message,
                canonical_json(details),
            ),
        )
        return event_id

    def clock_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM cluster_state WHERE singleton = 1").fetchone()

    def state_epoch(self, connection: sqlite3.Connection) -> int:
        row = self.clock_row(connection)
        return int(row["epoch_counter"]) if row else 0

    def current_election(self, connection: sqlite3.Connection) -> dict[str, Any] | None:
        state = self.clock_row(connection)
        if state is None or state["election_id"] is None:
            return None
        row = connection.execute(
            "SELECT * FROM elections WHERE election_id = ?", (state["election_id"],)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["state_json"])

    def precision_pairs(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        state = self.clock_row(connection)
        if state is None or state["election_id"] is None:
            return []
        rows = connection.execute(
            """
            SELECT * FROM precision_records
            WHERE election_id = ?
            ORDER BY pair_key
            """,
            (state["election_id"],),
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "pair_key": row["pair_key"],
                    "node_a": row["node_a"],
                    "node_b": row["node_b"],
                    "score_a_units": int(row["score_a_units"]),
                    "score_b_units": int(row["score_b_units"]),
                    "gap_ms": row["gap_ms"],
                    "tolerance_ms": row["tolerance_ms"],
                    "error_source_chain": json.loads(row["error_source_chain_json"]),
                    "affected_candidates": json.loads(row["affected_candidates_json"]),
                    "first_rejection": json.loads(row["first_rejection_json"]),
                }
            )
        return result

    def status_summary(self, connection: sqlite3.Connection) -> dict[str, Any]:
        state = self.clock_row(connection)
        if state is None:
            return {
                "watermark_ms": "0",
                "leader": None,
                "election_id": None,
                "epoch_counter": 0,
            }
        return {
            "watermark_ms": state["clock_text"],
            "watermark_fixed_units": int(state["clock_units"]),
            "watermark_residual_num": state["clock_residual_num"],
            "watermark_residual_den": state["clock_residual_den"],
            "leader": state["leader"],
            "election_id": state["election_id"],
            "epoch_counter": int(state["epoch_counter"]),
        }

    def read_fraction(self, row: sqlite3.Row, prefix: str):
        from fractions import Fraction

        return Fraction(int(row[f"{prefix}_num"]), int(row[f"{prefix}_den"]))

    def fraction_text_for(self, value) -> str:
        return fraction_text(value)
