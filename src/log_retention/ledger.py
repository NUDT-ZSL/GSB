from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import re
import sqlite3
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any, Self

from .errors import fail
from .models import (
    InstanceReplayState,
    NormalizedInput,
    Policy,
    ReplayEvent,
    Transaction,
)

SCHEMA_VERSION = 1


SCHEMA_SQL = """
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_info (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS load_versions (
    load_version INTEGER PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    transaction_count INTEGER NOT NULL,
    replay_event_count INTEGER NOT NULL,
    policy_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS load_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    load_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS staging_primary (
    load_version INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    commit_ms INTEGER NOT NULL,
    byte_size INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    PRIMARY KEY (load_version, txn_id)
);

CREATE TABLE IF NOT EXISTS staging_replay (
    load_version INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    replay_ms INTEGER NOT NULL,
    result TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    PRIMARY KEY (load_version, event_seq)
);

CREATE TABLE IF NOT EXISTS staging_policies (
    load_version INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    segment_id INTEGER,
    retention_generation INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    absolute_cutoff_ms INTEGER,
    relative_seconds INTEGER,
    required_min_catchup_margin INTEGER NOT NULL,
    parent_policy_id TEXT,
    policy_tombstone INTEGER NOT NULL,
    retention_window TEXT,
    source_ref TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (load_version, source_version, policy_id)
);

CREATE TABLE IF NOT EXISTS primary_transactions (
    load_version INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    commit_ms INTEGER NOT NULL,
    byte_size INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    PRIMARY KEY (load_version, txn_id)
);
CREATE INDEX IF NOT EXISTS idx_primary_segment
    ON primary_transactions(load_version, segment_id, commit_ms);

CREATE TABLE IF NOT EXISTS replay_events (
    load_version INTEGER NOT NULL,
    event_seq INTEGER NOT NULL,
    txn_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    replay_ms INTEGER NOT NULL,
    result TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    PRIMARY KEY (load_version, event_seq)
);
CREATE INDEX IF NOT EXISTS idx_replay_instance
    ON replay_events(load_version, instance_id, txn_id);

CREATE TABLE IF NOT EXISTS replica_instances (
    load_version INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    latest_txn_id INTEGER NOT NULL,
    latest_replay_ms INTEGER NOT NULL,
    result TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    event_count INTEGER NOT NULL,
    PRIMARY KEY (load_version, instance_id)
);

CREATE TABLE IF NOT EXISTS policies (
    load_version INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    segment_id INTEGER,
    retention_generation INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    absolute_cutoff_ms INTEGER,
    relative_seconds INTEGER,
    required_min_catchup_margin INTEGER NOT NULL,
    parent_policy_id TEXT,
    policy_tombstone INTEGER NOT NULL,
    retention_window TEXT,
    source_ref TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (load_version, source_version, policy_id)
);
CREATE INDEX IF NOT EXISTS idx_policies_instance
    ON policies(load_version, instance_id, retention_generation);

CREATE TABLE IF NOT EXISTS view_versions (
    view_version INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    source_view_version INTEGER,
    load_version INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    requested_clock_ms INTEGER NOT NULL,
    archive_target_watermark_ms INTEGER NOT NULL,
    operation_id TEXT,
    fingerprint TEXT NOT NULL UNIQUE,
    content_hash TEXT NOT NULL,
    conflict_count INTEGER NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS view_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    view_version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS view_policies (
    view_version INTEGER NOT NULL,
    policy_key TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_policy_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    segment_id INTEGER,
    retention_generation INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    raw_cutoff_ms INTEGER NOT NULL,
    relative_seconds INTEGER,
    original_time_json TEXT NOT NULL,
    required_min_catchup_margin INTEGER NOT NULL,
    parent_policy_key TEXT,
    source_priority INTEGER NOT NULL,
    active INTEGER NOT NULL,
    selected INTEGER NOT NULL,
    effective INTEGER NOT NULL,
    tombstone INTEGER NOT NULL,
    invalidated INTEGER NOT NULL,
    invalidation_reason TEXT,
    merge_decision_json TEXT NOT NULL,
    merged_from_json TEXT NOT NULL,
    conversion_json TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    PRIMARY KEY (view_version, policy_key)
);
CREATE INDEX IF NOT EXISTS idx_view_policies_instance
    ON view_policies(view_version, instance_id, effective);

CREATE TABLE IF NOT EXISTS view_edges (
    view_version INTEGER NOT NULL,
    edge_order INTEGER NOT NULL,
    parent_type TEXT NOT NULL,
    parent_id TEXT NOT NULL,
    child_type TEXT NOT NULL,
    child_id TEXT NOT NULL,
    attributes_json TEXT NOT NULL,
    PRIMARY KEY (view_version, edge_order)
);

CREATE TABLE IF NOT EXISTS view_instance_state (
    view_version INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    cutoff_ms INTEGER NOT NULL,
    declared_min_units INTEGER NOT NULL,
    computed_margin_units INTEGER NOT NULL,
    margin_units INTEGER NOT NULL,
    carry_in_units INTEGER NOT NULL,
    remainder_units INTEGER NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    window_id INTEGER NOT NULL,
    calc_key TEXT NOT NULL,
    margin_json TEXT NOT NULL,
    PRIMARY KEY (view_version, instance_id, policy_key)
);

CREATE TABLE IF NOT EXISTS segment_decisions (
    view_version INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    decision TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    segment_min_txn INTEGER NOT NULL,
    segment_max_txn INTEGER NOT NULL,
    segment_min_commit_ms INTEGER NOT NULL,
    segment_max_commit_ms INTEGER NOT NULL,
    raw_min_retention_cutoff_ms INTEGER,
    conservative_retention_watermark_ms INTEGER,
    fixed_margin_units_total INTEGER NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    conflict_count INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    PRIMARY KEY (view_version, segment_id)
);

CREATE TABLE IF NOT EXISTS conflict_records (
    conflict_id INTEGER PRIMARY KEY AUTOINCREMENT,
    view_version INTEGER NOT NULL,
    segment_id INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    winning_policy_key TEXT NOT NULL,
    losing_policy_keys_json TEXT NOT NULL,
    common_ancestor_policy_key TEXT,
    resolved_in_rebuild INTEGER NOT NULL,
    evidence_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    load_version INTEGER NOT NULL,
    policy_key TEXT NOT NULL,
    source_version TEXT NOT NULL,
    source_policy_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    segment_id INTEGER,
    retention_generation INTEGER NOT NULL,
    state TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    view_version INTEGER NOT NULL,
    related_policy_key TEXT,
    reason TEXT,
    evidence_json TEXT NOT NULL,
    UNIQUE(load_version, policy_key, state, view_version)
);

CREATE TABLE IF NOT EXISTS policy_invalidations (
    invalidation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    load_version INTEGER NOT NULL,
    policy_key TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    segment_id INTEGER,
    invalidated_by_policy_key TEXT NOT NULL,
    common_ancestor_policy_key TEXT,
    reason TEXT NOT NULL,
    view_version INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE(load_version, policy_key)
);

CREATE TABLE IF NOT EXISTS rebuild_records (
    rebuild_id INTEGER PRIMARY KEY AUTOINCREMENT,
    view_version INTEGER NOT NULL,
    source_view_version INTEGER NOT NULL,
    conflict_id INTEGER NOT NULL,
    invalidated_policy_keys_json TEXT NOT NULL,
    common_ancestor_policy_key TEXT,
    basis_json TEXT NOT NULL,
    clean_equivalence_json TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    load_version INTEGER NOT NULL,
    source_view_version INTEGER,
    requested_clock_ms INTEGER NOT NULL,
    effective_clock_ms INTEGER NOT NULL,
    archive_target_ms INTEGER NOT NULL,
    expected_instances_json TEXT NOT NULL,
    status TEXT NOT NULL,
    view_version INTEGER,
    error_code TEXT,
    error_json TEXT,
    UNIQUE(kind, fingerprint, load_version)
);

CREATE TABLE IF NOT EXISTS retention_windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    input_signature TEXT NOT NULL,
    lag_bytes INTEGER NOT NULL,
    replay_rate_bytes_per_sec INTEGER NOT NULL,
    scale_units_per_ms INTEGER NOT NULL,
    declared_min_units INTEGER NOT NULL,
    carry_in_units INTEGER NOT NULL,
    computed_margin_units INTEGER NOT NULL,
    effective_units INTEGER NOT NULL,
    remainder_units INTEGER NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    UNIQUE(instance_id, policy_generation, logical_clock_ms, input_signature)
);

CREATE TABLE IF NOT EXISTS op_calculations (
    op_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    policy_generation INTEGER NOT NULL,
    window_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (op_id, instance_id, policy_key)
);

CREATE TABLE IF NOT EXISTS operation_results (
    op_id TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rebuild_stages (
    op_id TEXT NOT NULL,
    policy_key TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    segment_id INTEGER,
    winning_policy_key TEXT NOT NULL,
    common_ancestor_policy_key TEXT,
    conflict_id INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (op_id, policy_key)
);

CREATE INDEX IF NOT EXISTS idx_conflicts_view
    ON conflict_records(view_version, resolved_in_rebuild);
CREATE INDEX IF NOT EXISTS idx_operations_status
    ON operations(status, kind);
CREATE INDEX IF NOT EXISTS idx_windows_instance_clock
    ON retention_windows(instance_id, logical_clock_ms);
"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _opaque(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:120]


def _as_bool(value: Any) -> bool:
    return bool(int(value))


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / "locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_info(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: types.TracebackType | None,
    ) -> None:
        self.close()

    @contextlib.contextmanager
    def transaction(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    @contextlib.contextmanager
    def read_transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN")
        try:
            yield self.conn
        finally:
            self.conn.execute("COMMIT")

    @contextlib.contextmanager
    def global_lock(self) -> Iterator[None]:
        path = self.lock_dir / "ledger.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @contextlib.contextmanager
    def instance_lock(self, instance_id: str) -> Iterator[None]:
        path = self.lock_dir / f"instance-{_opaque(instance_id)}.lock"
        with path.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    @staticmethod
    def meta_set(conn: sqlite3.Connection, key: str, value: str | int) -> None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def clock_watermark(self, conn: sqlite3.Connection) -> int | None:
        value = self.meta_get(conn, "logical_clock_watermark_ms")
        return None if value is None else int(value)

    def current_load_version(self, conn: sqlite3.Connection | None = None) -> int:
        connection = conn or self.conn
        row = connection.execute(
            "SELECT load_version FROM load_pointer WHERE singleton = 1"
        ).fetchone()
        if row is None:
            fail("E_NOT_LOADED", "no policy/stream load has been committed")
        return int(row["load_version"])

    def current_view_version(
        self, conn: sqlite3.Connection | None = None
    ) -> int | None:
        connection = conn or self.conn
        row = connection.execute(
            "SELECT view_version FROM view_pointer WHERE singleton = 1"
        ).fetchone()
        return None if row is None else int(row["view_version"])

    def load_input(
        self, normalized: NormalizedInput, load_version: int
    ) -> dict[str, Any]:
        with self.global_lock(), self.transaction() as conn:
            current = conn.execute(
                "SELECT load_version FROM load_pointer WHERE singleton = 1"
            ).fetchone()
            current_version = None if current is None else int(current["load_version"])
            existing = conn.execute(
                "SELECT fingerprint, transaction_count, replay_event_count, policy_count "
                "FROM load_versions WHERE load_version = ?",
                (load_version,),
            ).fetchone()
            if current_version is not None and load_version < current_version:
                fail(
                    "E_LOAD_VERSION_REGRESSION",
                    "load version cannot move backwards",
                    requested=load_version,
                    current=current_version,
                )
            if existing is not None:
                if existing["fingerprint"] == normalized.fingerprint:
                    return {
                        "load_version": load_version,
                        "idempotent": True,
                        "fingerprint": normalized.fingerprint,
                        "current": current_version == load_version,
                    }
                fail(
                    "E_LOAD_VERSION_REUSE",
                    "load version was already used by different input",
                    load_version=load_version,
                    existing_fingerprint=existing["fingerprint"],
                    incoming_fingerprint=normalized.fingerprint,
                )

            conn.execute(
                "DELETE FROM staging_primary WHERE load_version = ?", (load_version,)
            )
            conn.execute(
                "DELETE FROM staging_replay WHERE load_version = ?", (load_version,)
            )
            conn.execute(
                "DELETE FROM staging_policies WHERE load_version = ?", (load_version,)
            )
            conn.executemany(
                "INSERT INTO staging_primary(load_version, txn_id, commit_ms, byte_size, segment_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (load_version, t.txn_id, t.commit_ms, t.byte_size, t.segment_id)
                    for t in normalized.transactions
                ],
            )
            conn.executemany(
                "INSERT INTO staging_replay(load_version, event_seq, txn_id, instance_id, "
                "replay_ms, result, tombstone) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        load_version,
                        e.sequence,
                        e.txn_id,
                        e.instance_id,
                        e.replay_ms,
                        e.result,
                        1 if e.tombstone else 0,
                    )
                    for e in normalized.replay_events
                ],
            )
            conn.executemany(
                "INSERT INTO staging_policies("
                "load_version, source_version, policy_id, instance_id, segment_id, "
                "retention_generation, effective_at_ms, absolute_cutoff_ms, relative_seconds, "
                "required_min_catchup_margin, parent_policy_id, policy_tombstone, "
                "retention_window, source_ref, source_priority, raw_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        load_version,
                        p.source_version,
                        p.policy_id,
                        p.instance_id,
                        p.segment_id,
                        p.retention_generation,
                        p.effective_at_ms,
                        p.absolute_cutoff_ms,
                        p.relative_seconds,
                        p.required_min_catchup_margin,
                        p.parent_policy_id,
                        1 if p.tombstone else 0,
                        p.retention_window,
                        p.source_ref,
                        p.source_priority,
                        _json(p.raw),
                    )
                    for p in normalized.policies
                ],
            )
            conn.execute(
                "INSERT INTO primary_transactions(load_version, txn_id, commit_ms, byte_size, segment_id) "
                "SELECT load_version, txn_id, commit_ms, byte_size, segment_id "
                "FROM staging_primary WHERE load_version = ?",
                (load_version,),
            )
            conn.execute(
                "INSERT INTO replay_events(load_version, event_seq, txn_id, instance_id, "
                "replay_ms, result, tombstone) "
                "SELECT load_version, event_seq, txn_id, instance_id, replay_ms, result, tombstone "
                "FROM staging_replay WHERE load_version = ?",
                (load_version,),
            )
            conn.execute(
                "INSERT INTO policies("
                "load_version, source_version, policy_id, instance_id, segment_id, "
                "retention_generation, effective_at_ms, absolute_cutoff_ms, relative_seconds, "
                "required_min_catchup_margin, parent_policy_id, policy_tombstone, "
                "retention_window, source_ref, source_priority, raw_json) "
                "SELECT load_version, source_version, policy_id, instance_id, segment_id, "
                "retention_generation, effective_at_ms, absolute_cutoff_ms, relative_seconds, "
                "required_min_catchup_margin, parent_policy_id, policy_tombstone, "
                "retention_window, source_ref, source_priority, raw_json "
                "FROM staging_policies WHERE load_version = ?",
                (load_version,),
            )
            conn.executemany(
                "INSERT INTO replica_instances(load_version, instance_id, latest_txn_id, "
                "latest_replay_ms, result, tombstone, event_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        load_version,
                        state.instance_id,
                        state.latest_txn_id,
                        state.latest_replay_ms,
                        state.result,
                        1 if state.tombstone else 0,
                        state.event_count,
                    )
                    for state in sorted(
                        normalized.replay_states.values(),
                        key=lambda item: item.instance_id,
                    )
                ],
            )
            conn.execute(
                "INSERT INTO load_versions(load_version, fingerprint, transaction_count, "
                "replay_event_count, policy_count) VALUES (?, ?, ?, ?, ?)",
                (
                    load_version,
                    normalized.fingerprint,
                    len(normalized.transactions),
                    len(normalized.replay_events),
                    len(normalized.policies),
                ),
            )
            for policy in normalized.policies:
                conn.execute(
                    "INSERT INTO policy_history("
                    "load_version, policy_key, source_version, source_policy_id, instance_id, "
                    "segment_id, retention_generation, state, tombstone, view_version, "
                    "related_policy_key, reason, evidence_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 'loaded', ?, 0, NULL, 'input_policy', ?)",
                    (
                        load_version,
                        policy.key,
                        policy.source_version,
                        policy.policy_id,
                        policy.instance_id,
                        policy.segment_id,
                        policy.retention_generation,
                        1 if policy.tombstone else 0,
                        _json(
                            {
                                "source_ref": policy.source_ref,
                                "raw": policy.raw,
                            }
                        ),
                    ),
                )
            conn.execute(
                "DELETE FROM staging_primary WHERE load_version = ?", (load_version,)
            )
            conn.execute(
                "DELETE FROM staging_replay WHERE load_version = ?", (load_version,)
            )
            conn.execute(
                "DELETE FROM staging_policies WHERE load_version = ?", (load_version,)
            )
            conn.execute(
                "INSERT INTO load_pointer(singleton, load_version) VALUES (1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET load_version=excluded.load_version",
                (load_version,),
            )
            conn.execute("DELETE FROM view_pointer")
            conn.execute(
                "UPDATE operations SET status='aborted', error_code='E_LOAD_REPLACED' "
                "WHERE status IN ('staging', 'finalizing')"
            )
            return {
                "load_version": load_version,
                "idempotent": False,
                "fingerprint": normalized.fingerprint,
                "transactions": len(normalized.transactions),
                "replay_events": len(normalized.replay_events),
                "policies": len(normalized.policies),
                "segments": normalized.segment_ids,
            }

    def read_snapshot(
        self, load_version: int | None = None, conn: sqlite3.Connection | None = None
    ) -> NormalizedInput:
        connection = conn or self.conn
        version = (
            self.current_load_version(connection)
            if load_version is None
            else load_version
        )
        version_row = connection.execute(
            "SELECT fingerprint FROM load_versions WHERE load_version = ?", (version,)
        ).fetchone()
        if version_row is None:
            fail(
                "E_NOT_LOADED",
                "requested load version does not exist",
                load_version=version,
            )
        transactions = [
            Transaction(
                int(row["txn_id"]),
                int(row["commit_ms"]),
                int(row["byte_size"]),
                int(row["segment_id"]),
            )
            for row in connection.execute(
                "SELECT txn_id, commit_ms, byte_size, segment_id "
                "FROM primary_transactions WHERE load_version = ? ORDER BY txn_id",
                (version,),
            )
        ]
        replay_events = [
            ReplayEvent(
                int(row["event_seq"]),
                int(row["txn_id"]),
                str(row["instance_id"]),
                int(row["replay_ms"]),
                str(row["result"]),
                _as_bool(row["tombstone"]),
            )
            for row in connection.execute(
                "SELECT event_seq, txn_id, instance_id, replay_ms, result, tombstone "
                "FROM replay_events WHERE load_version = ? ORDER BY event_seq",
                (version,),
            )
        ]
        replay_states = {
            str(row["instance_id"]): InstanceReplayState(
                instance_id=str(row["instance_id"]),
                latest_txn_id=int(row["latest_txn_id"]),
                latest_replay_ms=int(row["latest_replay_ms"]),
                result=str(row["result"]),
                tombstone=_as_bool(row["tombstone"]),
                event_count=int(row["event_count"]),
            )
            for row in connection.execute(
                "SELECT instance_id, latest_txn_id, latest_replay_ms, result, tombstone, "
                "event_count FROM replica_instances WHERE load_version = ?",
                (version,),
            )
        }
        policies = [
            Policy(
                policy_id=str(row["policy_id"]),
                source_version=str(row["source_version"]),
                instance_id=str(row["instance_id"]),
                segment_id=None
                if row["segment_id"] is None
                else int(row["segment_id"]),
                retention_generation=int(row["retention_generation"]),
                effective_at_ms=int(row["effective_at_ms"]),
                relative_seconds=(
                    None
                    if row["relative_seconds"] is None
                    else int(row["relative_seconds"])
                ),
                absolute_cutoff_ms=(
                    None
                    if row["absolute_cutoff_ms"] is None
                    else int(row["absolute_cutoff_ms"])
                ),
                required_min_catchup_margin=int(row["required_min_catchup_margin"]),
                parent_policy_id=(
                    None
                    if row["parent_policy_id"] is None
                    else str(row["parent_policy_id"])
                ),
                tombstone=_as_bool(row["policy_tombstone"]),
                retention_window=(
                    None
                    if row["retention_window"] is None
                    else str(row["retention_window"])
                ),
                source_ref=str(row["source_ref"]),
                source_priority=int(row["source_priority"]),
                raw=json.loads(str(row["raw_json"])),
            )
            for row in connection.execute(
                "SELECT * FROM policies WHERE load_version = ? "
                "ORDER BY source_version, policy_id",
                (version,),
            )
        ]
        return NormalizedInput(
            transactions=transactions,
            replay_events=replay_events,
            replay_states=replay_states,
            policies=policies,
            segment_ids=sorted({txn.segment_id for txn in transactions}),
            fingerprint=str(version_row["fingerprint"]),
        )

    def invalidations(
        self, load_version: int, conn: sqlite3.Connection | None = None
    ) -> set[str]:
        connection = conn or self.conn
        return {
            str(row["policy_key"])
            for row in connection.execute(
                "SELECT policy_key FROM policy_invalidations WHERE load_version = ?",
                (load_version,),
            )
        }

    def allocate_view_version_and_insert(
        self,
        conn: sqlite3.Connection,
        *,
        kind: str,
        source_view_version: int | None,
        load_version: int,
        logical_clock_ms: int,
        requested_clock_ms: int,
        archive_target_ms: int,
        operation_id: str | None,
        fingerprint: str,
        content_hash: str,
        conflict_count: int,
        status: str = "committed",
    ) -> int:
        try:
            cursor = conn.execute(
                "INSERT INTO view_versions(kind, source_view_version, load_version, "
                "logical_clock_ms, requested_clock_ms, archive_target_watermark_ms, "
                "operation_id, fingerprint, content_hash, conflict_count, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    kind,
                    source_view_version,
                    load_version,
                    logical_clock_ms,
                    requested_clock_ms,
                    archive_target_ms,
                    operation_id,
                    fingerprint,
                    content_hash,
                    conflict_count,
                    status,
                ),
            )
        except sqlite3.IntegrityError:
            row = conn.execute(
                "SELECT view_version FROM view_versions WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                raise
            return int(row["view_version"])
        return int(cursor.lastrowid)

    def insert_view_policies(
        self,
        conn: sqlite3.Connection,
        view_version: int,
        policy_rows: list[dict[str, Any]],
    ) -> None:
        conn.executemany(
            "INSERT INTO view_policies("
            "view_version, policy_key, source_version, source_policy_id, instance_id, "
            "segment_id, retention_generation, effective_at_ms, raw_cutoff_ms, relative_seconds, "
            "original_time_json, required_min_catchup_margin, parent_policy_key, source_priority, "
            "active, selected, effective, tombstone, invalidated, invalidation_reason, "
            "merge_decision_json, merged_from_json, conversion_json, raw_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    view_version,
                    row["policy_key"],
                    row["source_version"],
                    row["source_policy_id"],
                    row["instance_id"],
                    row["segment_id"],
                    row["retention_generation"],
                    row["effective_at_ms"],
                    row["raw_cutoff_ms"],
                    row["relative_seconds"],
                    _json(row["original_time"]),
                    row["required_min_catchup_margin"],
                    row["parent_policy_key"],
                    row["source_priority"],
                    int(bool(row["active"])),
                    int(bool(row["selected"])),
                    int(bool(row["effective"])),
                    int(bool(row["tombstone"])),
                    int(bool(row["invalidated"])),
                    row["invalidation_reason"],
                    _json(row["merge_decision"]),
                    _json(row["merged_from"]),
                    _json(row["conversion"]),
                    _json(row["raw"]),
                )
                for row in policy_rows
            ],
        )

    def insert_view_edges(
        self, conn: sqlite3.Connection, view_version: int, edges: list[dict[str, Any]]
    ) -> None:
        conn.executemany(
            "INSERT INTO view_edges(view_version, edge_order, parent_type, parent_id, "
            "child_type, child_id, attributes_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    view_version,
                    edge["edge_order"],
                    edge["parent_type"],
                    edge["parent_id"],
                    edge["child_type"],
                    edge["child_id"],
                    _json(edge["attributes"]),
                )
                for edge in edges
            ],
        )

    def insert_instance_states(
        self, conn: sqlite3.Connection, view_version: int, rows: list[dict[str, Any]]
    ) -> None:
        conn.executemany(
            "INSERT INTO view_instance_state("
            "view_version, instance_id, policy_key, policy_generation, cutoff_ms, "
            "declared_min_units, computed_margin_units, margin_units, carry_in_units, "
            "remainder_units, precision_sensitive, window_id, calc_key, margin_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    view_version,
                    row["instance_id"],
                    row["policy_key"],
                    row["policy_generation"],
                    row["cutoff_ms"],
                    row["declared_min_units"],
                    row["computed_margin_units"],
                    row["margin_units"],
                    row["carry_in_units"],
                    row["remainder_units"],
                    int(bool(row["precision_sensitive"])),
                    row["window_id"],
                    row["calc_key"],
                    _json(row["margin_json"]),
                )
                for row in rows
            ],
        )

    def insert_decisions(
        self,
        conn: sqlite3.Connection,
        view_version: int,
        decisions: list[dict[str, Any]],
    ) -> None:
        conn.executemany(
            "INSERT INTO segment_decisions("
            "view_version, segment_id, decision, reason_code, segment_min_txn, segment_max_txn, "
            "segment_min_commit_ms, segment_max_commit_ms, raw_min_retention_cutoff_ms, "
            "conservative_retention_watermark_ms, fixed_margin_units_total, precision_sensitive, "
            "conflict_count, evidence_json, decision_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    view_version,
                    row["segment_id"],
                    row["decision"],
                    row["reason_code"],
                    row["segment_min_txn"],
                    row["segment_max_txn"],
                    row["segment_min_commit_ms"],
                    row["segment_max_commit_ms"],
                    row.get("raw_min_retention_cutoff_ms"),
                    row.get("conservative_retention_watermark_ms"),
                    row["fixed_margin_units_total"],
                    int(bool(row["precision_sensitive"])),
                    row["conflict_count"],
                    _json(row["evidence"]),
                    row["decision_hash"],
                )
                for row in decisions
            ],
        )

    def insert_conflicts(
        self,
        conn: sqlite3.Connection,
        view_version: int,
        conflicts: list[dict[str, Any]],
        *,
        resolved: bool = False,
    ) -> None:
        conn.executemany(
            "INSERT INTO conflict_records("
            "view_version, segment_id, instance_id, winning_policy_key, "
            "losing_policy_keys_json, common_ancestor_policy_key, resolved_in_rebuild, "
            "evidence_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    view_version,
                    conflict["segment_id"],
                    conflict["instance_id"],
                    conflict["winning_policy_key"],
                    _json(conflict["losing_policy_keys"]),
                    conflict["common_ancestor_policy_key"],
                    int(resolved),
                    _json(conflict["evidence"]),
                )
                for conflict in conflicts
            ],
        )

    def set_view_pointer(self, conn: sqlite3.Connection, view_version: int) -> None:
        conn.execute(
            "INSERT INTO view_pointer(singleton, view_version) VALUES (1, ?) "
            "ON CONFLICT(singleton) DO UPDATE SET view_version=excluded.view_version",
            (view_version,),
        )

    def current_view(
        self, conn: sqlite3.Connection | None = None
    ) -> dict[str, Any] | None:
        connection = conn or self.conn
        view_version = self.current_view_version(connection)
        if view_version is None:
            return None
        row = connection.execute(
            "SELECT * FROM view_versions WHERE view_version = ?", (view_version,)
        ).fetchone()
        return None if row is None else dict(row)

    @staticmethod
    def latest_remainder(
        conn: sqlite3.Connection, instance_id: str, before_clock_ms: int
    ) -> int:
        row = conn.execute(
            "SELECT remainder_units FROM retention_windows "
            "WHERE instance_id = ? AND logical_clock_ms < ? "
            "ORDER BY logical_clock_ms DESC, window_id DESC LIMIT 1",
            (instance_id, before_clock_ms),
        ).fetchone()
        return 0 if row is None else int(row["remainder_units"])

    @staticmethod
    def find_window(
        conn: sqlite3.Connection,
        *,
        instance_id: str,
        policy_generation: int,
        logical_clock_ms: int,
        input_signature: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM retention_windows WHERE instance_id = ? "
            "AND policy_generation = ? AND logical_clock_ms = ? AND input_signature = ?",
            (instance_id, policy_generation, logical_clock_ms, input_signature),
        ).fetchone()

    @staticmethod
    def insert_window(
        conn: sqlite3.Connection,
        *,
        instance_id: str,
        policy_generation: int,
        logical_clock_ms: int,
        input_signature: str,
        lag_bytes: int,
        replay_rate: int,
        scale_units_per_ms: int,
        declared_min_units: int,
        carry_in_units: int,
        computed_margin_units: int,
        effective_units: int,
        remainder_units: int,
        precision_sensitive: bool,
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO retention_windows("
            "instance_id, policy_generation, logical_clock_ms, input_signature, lag_bytes, "
            "replay_rate_bytes_per_sec, scale_units_per_ms, declared_min_units, carry_in_units, "
            "computed_margin_units, effective_units, remainder_units, precision_sensitive) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                instance_id,
                policy_generation,
                logical_clock_ms,
                input_signature,
                lag_bytes,
                replay_rate,
                scale_units_per_ms,
                declared_min_units,
                carry_in_units,
                computed_margin_units,
                effective_units,
                remainder_units,
                int(precision_sensitive),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def result_hash(value: Any) -> str:
        return _hash_json(value)
