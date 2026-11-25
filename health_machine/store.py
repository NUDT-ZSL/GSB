"""SQLite persistence and cross-process locking primitives."""
from __future__ import annotations

import contextlib
import fcntl
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .errors import StorageError


SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS services (
        service_id TEXT PRIMARY KEY,
        service_name TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instances (
        instance_id TEXT PRIMARY KEY,
        service_id TEXT NOT NULL,
        service_name TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        state_generation INTEGER NOT NULL,
        levels_json TEXT NOT NULL,
        window_ms INTEGER NOT NULL,
        timeout_ms INTEGER NOT NULL,
        registry_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS observation_inbox (
        instance_id TEXT NOT NULL,
        level INTEGER NOT NULL,
        seq INTEGER NOT NULL,
        stream_seq INTEGER NOT NULL,
        window_index INTEGER NOT NULL,
        clock_ms INTEGER NOT NULL,
        successes INTEGER NOT NULL,
        failures INTEGER NOT NULL,
        latency_text TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'received',
        committed_window_index INTEGER,
        PRIMARY KEY (instance_id, level, seq)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_inbox_instance_window ON observation_inbox(instance_id, window_index)",
    "CREATE INDEX IF NOT EXISTS idx_inbox_status ON observation_inbox(instance_id, status)",
    """
    CREATE TABLE IF NOT EXISTS threshold_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        format TEXT NOT NULL,
        logical_clock_ms INTEGER NOT NULL,
        threshold_generation INTEGER NOT NULL,
        source_path TEXT,
        content_hash TEXT NOT NULL,
        raw_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS threshold_candidates (
        instance_id TEXT NOT NULL,
        level INTEGER NOT NULL,
        source_format TEXT NOT NULL,
        source_snapshot_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        logical_clock_ms INTEGER NOT NULL,
        effective_at_ms INTEGER NOT NULL,
        weight INTEGER NOT NULL,
        failure_limit INTEGER NOT NULL,
        min_score_bp INTEGER NOT NULL,
        latency_budget_ms TEXT NOT NULL,
        failure_counts_json TEXT NOT NULL,
        time_conversion_json TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        PRIMARY KEY (instance_id, level, source_format, source_snapshot_id, generation)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS threshold_view (
        instance_id TEXT NOT NULL,
        level INTEGER NOT NULL,
        winner_format TEXT NOT NULL,
        winner_snapshot_id TEXT NOT NULL,
        generation INTEGER NOT NULL,
        logical_clock_ms INTEGER NOT NULL,
        effective_at_ms INTEGER NOT NULL,
        weight INTEGER NOT NULL,
        failure_limit INTEGER NOT NULL,
        min_score_bp INTEGER NOT NULL,
        latency_budget_ms TEXT NOT NULL,
        active INTEGER NOT NULL,
        exclusion_reason TEXT,
        excluded_generation INTEGER,
        raw_failure_counts_json TEXT NOT NULL,
        time_conversion_json TEXT NOT NULL,
        provenance_json TEXT NOT NULL,
        decision_json TEXT NOT NULL,
        view_revision INTEGER NOT NULL,
        content_hash TEXT NOT NULL,
        updated_patch_id TEXT,
        PRIMARY KEY (instance_id, level)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS unified_pointer (
        instance_id TEXT PRIMARY KEY,
        view_revision INTEGER NOT NULL,
        view_hash TEXT NOT NULL,
        threshold_generation INTEGER NOT NULL,
        updated_patch_id TEXT,
        updated_clock_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS window_summaries (
        instance_id TEXT NOT NULL,
        window_index INTEGER NOT NULL,
        window_start_ms INTEGER NOT NULL,
        window_end_ms INTEGER NOT NULL,
        score_bp INTEGER NOT NULL,
        healthy INTEGER NOT NULL,
        failed INTEGER NOT NULL,
        timed_out INTEGER NOT NULL,
        trigger_level INTEGER,
        trigger_kind TEXT,
        trigger_threshold_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        excluded_thresholds_json TEXT NOT NULL,
        threshold_hash TEXT NOT NULL,
        input_hash TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        PRIMARY KEY (instance_id, window_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS window_level_scores (
        instance_id TEXT NOT NULL,
        window_index INTEGER NOT NULL,
        level INTEGER NOT NULL,
        score_bp INTEGER NOT NULL,
        availability_bp INTEGER NOT NULL,
        latency_score_bp INTEGER NOT NULL,
        weight INTEGER NOT NULL,
        successes INTEGER NOT NULL,
        failures INTEGER NOT NULL,
        total_probes INTEGER NOT NULL,
        latency_text TEXT NOT NULL,
        breached INTEGER NOT NULL,
        timeout_breached INTEGER NOT NULL,
        observation_seqs_json TEXT NOT NULL,
        detail_json TEXT NOT NULL,
        PRIMARY KEY (instance_id, window_index, level)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instance_state (
        instance_id TEXT PRIMARY KEY,
        state TEXT NOT NULL,
        failure_streak INTEGER NOT NULL,
        recovery_streak INTEGER NOT NULL,
        recovery_arc_json TEXT NOT NULL,
        recovery_start_clock_ms INTEGER,
        recovery_last_clock_ms INTEGER,
        recovery_duration_ms INTEGER NOT NULL,
        last_window_index INTEGER,
        last_clock_ms INTEGER NOT NULL,
        committed_seqs_json TEXT NOT NULL,
        committed_stream_seq INTEGER NOT NULL,
        threshold_revision INTEGER,
        threshold_hash TEXT,
        threshold_generation INTEGER,
        state_version INTEGER NOT NULL,
        state_hash TEXT NOT NULL,
        updated_patch_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS transition_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id TEXT NOT NULL,
        transition_seq INTEGER NOT NULL,
        from_state TEXT NOT NULL,
        to_state TEXT NOT NULL,
        window_index INTEGER NOT NULL,
        at_clock_ms INTEGER NOT NULL,
        trigger_level INTEGER,
        trigger_kind TEXT NOT NULL,
        trigger_threshold_json TEXT NOT NULL,
        evidence_json TEXT NOT NULL,
        reason TEXT NOT NULL,
        prev_hash TEXT NOT NULL,
        entry_hash TEXT NOT NULL,
        patch_id TEXT,
        UNIQUE(instance_id, transition_seq),
        UNIQUE(instance_id, window_index, from_state, to_state)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recovery_arcs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        instance_id TEXT NOT NULL,
        arc_seq INTEGER NOT NULL,
        status TEXT NOT NULL,
        started_clock_ms INTEGER NOT NULL,
        ended_clock_ms INTEGER,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        required_windows INTEGER NOT NULL,
        required_duration_ms INTEGER NOT NULL,
        window_indices_json TEXT NOT NULL,
        result_state TEXT,
        UNIQUE(instance_id, arc_seq)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS patches (
        patch_id TEXT PRIMARY KEY,
        patch_hash TEXT NOT NULL UNIQUE,
        patch_type TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        result_json TEXT NOT NULL,
        applied_clock_ms INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS patch_closures (
        patch_id TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        scope_level INTEGER,
        scope TEXT NOT NULL,
        reason TEXT NOT NULL,
        recomputed INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (patch_id, instance_id, scope_level, scope)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recompute_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        patch_id TEXT,
        run_reason TEXT NOT NULL,
        instance_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        window_count INTEGER NOT NULL,
        created_order INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot_versions (
        snapshot_version TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS idempotent_commands (
        command_id TEXT PRIMARY KEY,
        command_type TEXT NOT NULL,
        request_hash TEXT NOT NULL,
        result_json TEXT NOT NULL
    )
    """,
]


class Store:
    def __init__(self, path: str | Path = "state/hsm.sqlite") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        return conn

    def _init_schema(self) -> None:
        with self.file_lock("_schema"):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA_STATEMENTS:
                    conn.execute(statement)
                conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()

    @contextlib.contextmanager
    def file_lock(self, key: str) -> Iterator[None]:
        safe_key = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in key)
        lock_path = self.lock_dir / f"{safe_key}.lock"
        with lock_path.open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield None
            except OSError as exc:
                raise StorageError(f"failed to acquire file lock {lock_path}: {exc}") from exc
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def transaction(self, lock_key: str = "_global") -> Iterator[sqlite3.Connection]:
        with self.file_lock(lock_key):
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                conn.close()

    def read_connection(self) -> sqlite3.Connection:
        return self._connect()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}
