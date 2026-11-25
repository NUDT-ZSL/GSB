from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .errors import ChainError
from .util import file_lock, require_safe_name


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS logical_tables (
        table_name TEXT PRIMARY KEY,
        physical_name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_packages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        package_key TEXT NOT NULL UNIQUE,
        manifest_version INTEGER NOT NULL,
        generation_min INTEGER NOT NULL,
        generation_max INTEGER NOT NULL,
        set_digest TEXT NOT NULL,
        decision TEXT NOT NULL,
        clock_ms INTEGER NOT NULL,
        manifest_json TEXT NOT NULL,
        created_sequence INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pointer_state (
        name TEXT PRIMARY KEY,
        view_id TEXT NOT NULL,
        view_table TEXT NOT NULL,
        version_integer INTEGER NOT NULL,
        clock_ms INTEGER NOT NULL,
        chain_digest TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chain_views (
        view_id TEXT PRIMARY KEY,
        clock_ms INTEGER NOT NULL,
        chain_digest TEXT NOT NULL UNIQUE,
        view_table TEXT NOT NULL UNIQUE,
        generation_min INTEGER NOT NULL,
        generation_max INTEGER NOT NULL,
        status TEXT NOT NULL,
        created_sequence INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS merge_decisions (
        view_id TEXT NOT NULL,
        clock_ms INTEGER NOT NULL,
        entry_id TEXT NOT NULL,
        selected_source TEXT NOT NULL,
        winner_key TEXT NOT NULL,
        reason TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        PRIMARY KEY(view_id, entry_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS applied_segments (
        segment_id TEXT PRIMARY KEY,
        generation INTEGER NOT NULL,
        version_integer INTEGER NOT NULL,
        digest TEXT NOT NULL,
        status TEXT NOT NULL,
        record_count INTEGER NOT NULL,
        clock_ms INTEGER NOT NULL,
        applied_clock_ms INTEGER,
        basis_json TEXT NOT NULL,
        UNIQUE(generation, version_integer, digest)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS applied_mutations (
        segment_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        txn_seq INTEGER NOT NULL,
        table_name TEXT NOT NULL,
        row_key TEXT NOT NULL,
        digest TEXT NOT NULL,
        PRIMARY KEY(segment_id, txn_seq, table_name, row_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS replay_runs (
        run_id TEXT PRIMARY KEY,
        view_id TEXT NOT NULL,
        bound_generation_max INTEGER NOT NULL,
        clock_ms INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_basis TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ledger_snapshots (
        view_id TEXT PRIMARY KEY,
        clock_ms INTEGER NOT NULL,
        chain_digest TEXT NOT NULL,
        ledger_digest TEXT NOT NULL UNIQUE,
        ledger_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS checkpoints (
        generation INTEGER PRIMARY KEY,
        checkpoint_id TEXT NOT NULL,
        commit_ms INTEGER NOT NULL,
        status TEXT NOT NULL,
        clock_ms INTEGER NOT NULL,
        ledger_digest TEXT,
        detail_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_runs (
        recovery_id TEXT PRIMARY KEY,
        sequence_no INTEGER NOT NULL UNIQUE,
        clock_ms INTEGER NOT NULL,
        status TEXT NOT NULL,
        conclusion TEXT NOT NULL,
        basis_json TEXT NOT NULL,
        ledger_digest TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        recovery_id TEXT NOT NULL,
        classification TEXT NOT NULL,
        path TEXT,
        digest TEXT,
        action TEXT NOT NULL,
        detail_json TEXT NOT NULL
    )
    """,
]


def quote_identifier(name: str) -> str:
    require_safe_name(name, "SQL identifier")
    return '"' + name.replace('"', '""') + '"'


def physical_table_name(logical_table: str) -> str:
    require_safe_name(logical_table, "table_name")
    return f"data__{logical_table}"


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=30000")
        if str(path) == ":memory:":
            self._initialize_database()
        else:
            with file_lock(self.path.parent / "locks" / "state-init.lock"):
                self._initialize_database()

    def _initialize_database(self) -> None:
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        with self.immediate():
            for statement in SCHEMA_STATEMENTS:
                self.conn.execute(statement)

    @contextlib.contextmanager
    def immediate(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def ensure_data_table(self, logical_table: str) -> str:
        physical = physical_table_name(logical_table)
        quoted = quote_identifier(physical)
        with self.immediate():
            self.conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {quoted} (
                    row_key TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    rv_version INTEGER NOT NULL,
                    rv_generation INTEGER NOT NULL,
                    rv_tombstone INTEGER NOT NULL,
                    source_segment_id TEXT NOT NULL,
                    txn_seq INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    updated_clock_ms INTEGER NOT NULL
                )
                """
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO logical_tables(table_name, physical_name) VALUES (?, ?)",
                (logical_table, physical),
            )
        return physical

    def logical_tables(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT table_name FROM logical_tables ORDER BY table_name"
        ).fetchall()
        return [str(row["table_name"]) for row in rows]

    def table_rows(self, logical_table: str) -> list[dict[str, Any]]:
        physical = self.conn.execute(
            "SELECT physical_name FROM logical_tables WHERE table_name = ?",
            (logical_table,),
        ).fetchone()
        if physical is None:
            return []
        quoted = quote_identifier(str(physical["physical_name"]))
        rows = self.conn.execute(
            f"SELECT row_key, payload_json, rv_version, rv_generation, rv_tombstone, "
            f"source_segment_id, txn_seq, digest, updated_clock_ms "
            f"FROM {quoted} ORDER BY row_key"
        ).fetchall()
        return [dict(row) for row in rows]

    def next_sequence(self, name: str) -> int:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (name,)).fetchone()
        if row is None:
            value = 1
            with self.immediate():
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES (?, ?)", (name, str(value + 1))
                )
            return value
        value = int(row["value"])
        with self.immediate():
            self.conn.execute("UPDATE meta SET value = ? WHERE key = ?", (str(value + 1), name))
        return value


def connect_state(root: Path) -> Store:
    state_path = root / "runtime" / "logchain.sqlite"
    try:
        return Store(state_path)
    except sqlite3.Error as exc:
        raise ChainError("STATE_DB_ERROR", "cannot open log-chain state database", {"error": str(exc)}) from exc
