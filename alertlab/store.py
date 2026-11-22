from __future__ import annotations

import contextlib
import fcntl
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import AlertLabError, invalid
from .events import EventSeed, EventStream
from .rules import (
    MergedRule,
    MergedSnapshot,
    SEVERITIES,
    migration_decision_json,
    migration_decision_key,
)
from .topology import Topology
from .util import canonical_json, sha256_json, sha256_text


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topology_snapshots (
    fingerprint TEXT PRIMARY KEY,
    document_json TEXT NOT NULL,
    loaded_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_views (
    view_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('snapshot', 'patch')),
    snapshot_version INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    parent_view_id INTEGER,
    base_fingerprint TEXT,
    patch_id TEXT,
    status TEXT NOT NULL,
    source_json TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS rule_views_snapshot_unique
    ON rule_views(snapshot_version, fingerprint)
    WHERE kind = 'snapshot';
CREATE UNIQUE INDEX IF NOT EXISTS rule_views_patch_unique
    ON rule_views(patch_id)
    WHERE patch_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS view_pointer (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    view_id INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    view_id INTEGER NOT NULL,
    rule_name TEXT NOT NULL,
    service TEXT NOT NULL,
    severity TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    tombstone INTEGER NOT NULL,
    source_version TEXT NOT NULL,
    generation INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (view_id, rule_name)
);

CREATE TABLE IF NOT EXISTS migration_decisions (
    decision_key TEXT PRIMARY KEY,
    source_version TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    generation INTEGER NOT NULL,
    source_clock_ms INTEGER NOT NULL,
    injected_clock_ms INTEGER NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    scale_ms_per_unit INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    decision_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS view_results (
    view_id INTEGER PRIMARY KEY,
    result_revision INTEGER NOT NULL UNIQUE,
    completed INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS streams (
    stream_id TEXT PRIMARY KEY,
    window_ms INTEGER NOT NULL,
    allowed_lateness_ms INTEGER NOT NULL,
    injected_clock_ms INTEGER NOT NULL,
    stream_hash TEXT NOT NULL,
    watermark_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_uid TEXT PRIMARY KEY,
    stream_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    instance_id TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    severity TEXT NOT NULL,
    event_ts_ms INTEGER NOT NULL,
    metric_value_json TEXT NOT NULL,
    labels_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    late INTEGER NOT NULL,
    status TEXT NOT NULL,
    signal_eligible INTEGER NOT NULL,
    first_seen_view_id INTEGER NOT NULL,
    UNIQUE (stream_id, seq)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    view_id INTEGER NOT NULL,
    stream_id TEXT NOT NULL,
    stream_hash TEXT NOT NULL,
    stream_json TEXT NOT NULL,
    injected_clock_ms INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    allowed_lateness_ms INTEGER NOT NULL,
    result_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    expected_cluster_ids_json TEXT NOT NULL,
    committed_clusters INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clusters (
    result_revision INTEGER NOT NULL,
    cluster_id TEXT NOT NULL,
    view_id INTEGER NOT NULL,
    rule_name TEXT NOT NULL,
    service TEXT NOT NULL,
    first_event_ms INTEGER NOT NULL,
    last_event_ms INTEGER NOT NULL,
    occurrence_count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL,
    silence_suppressed_count INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    summary_hash TEXT NOT NULL,
    PRIMARY KEY (result_revision, cluster_id),
    UNIQUE (view_id, cluster_id)
);

CREATE TABLE IF NOT EXISTS root_candidates (
    result_revision INTEGER NOT NULL,
    cluster_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    candidate_json TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    PRIMARY KEY (result_revision, cluster_id, candidate_id)
);

CREATE TABLE IF NOT EXISTS patches (
    patch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    base_view_id INTEGER NOT NULL,
    new_view_id INTEGER NOT NULL,
    result_revision INTEGER NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    changes_json TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT
);

CREATE TABLE IF NOT EXISTS invalidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_id TEXT NOT NULL,
    view_id INTEGER NOT NULL,
    result_revision INTEGER NOT NULL,
    trigger_source TEXT NOT NULL,
    trigger_kind TEXT NOT NULL,
    trigger_rule TEXT NOT NULL,
    reason TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    path TEXT NOT NULL,
    action TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    UNIQUE (patch_id, object_type, object_id, path, action)
);

CREATE TABLE IF NOT EXISTS commit_journal (
    run_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, object_type, object_id)
);

CREATE INDEX IF NOT EXISTS clusters_by_view ON clusters(view_id, result_revision);
CREATE INDEX IF NOT EXISTS events_by_rule ON events(rule_name, event_ts_ms, seq);
"""


class _LockState:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.local = threading.local()


_LOCK_STATES: dict[str, _LockState] = {}
_LOCK_STATES_GUARD = threading.Lock()


def _lock_state_for(path: Path) -> _LockState:
    key = str(path.resolve())
    with _LOCK_STATES_GUARD:
        state = _LOCK_STATES.get(key)
        if state is None:
            state = _LockState()
            _LOCK_STATES[key] = state
        return state


class Store:
    def __init__(self, database_path: str | Path, lock_path: str | Path | None = None):
        self.database_path = Path(database_path)
        self.lock_path = (
            Path(lock_path)
            if lock_path is not None
            else self.database_path.with_suffix(self.database_path.suffix + ".lock")
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_state = _lock_state_for(self.lock_path)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=15,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        state = self._lock_state
        with state.lock:
            depth = getattr(state.local, "depth", 0)
            if depth:
                state.local.depth = depth + 1
                try:
                    yield
                finally:
                    state.local.depth -= 1
                return
            handle = open(self.lock_path, "a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                state.local.depth = 1
                yield
            finally:
                state.local.depth = 0
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()

    @contextlib.contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.locked():
            connection = self.connect()
            try:
                connection.executescript(SCHEMA)
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('schema_version', '1')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('next_result_revision', '0')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('event_watermark_ms', '0')"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES ('logical_clock_watermark_ms', '0')"
                )
            finally:
                connection.close()

    @staticmethod
    def _meta(connection: sqlite3.Connection, key: str) -> str | None:
        row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, str(value)),
        )

    @staticmethod
    def _allocate_result_revision(connection: sqlite3.Connection) -> int:
        current = int(
            connection.execute(
                "SELECT value FROM meta WHERE key = 'next_result_revision'"
            ).fetchone()["value"]
        )
        revision = current + 1
        connection.execute(
            "UPDATE meta SET value = ? WHERE key = 'next_result_revision'",
            (str(revision),),
        )
        return revision

    @staticmethod
    def _new_view_id(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT COALESCE(MAX(view_id), 0) + 1 AS next_id FROM rule_views"
        ).fetchone()
        return int(row["next_id"])

    def _active_view_row(self, connection: sqlite3.Connection) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT rv.*
              FROM view_pointer vp
              JOIN rule_views rv ON rv.view_id = vp.view_id
             WHERE vp.singleton = 1
            """
        ).fetchone()

    def get_active_view(self) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = self._active_view_row(connection)
            return self._view_row_dict(row) if row else None
        finally:
            connection.close()

    @staticmethod
    def _view_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "view_id": row["view_id"],
            "kind": row["kind"],
            "snapshot_version": row["snapshot_version"],
            "logical_clock_ms": row["logical_clock_ms"],
            "fingerprint": row["fingerprint"],
            "source_fingerprint": row["source_fingerprint"],
            "parent_view_id": row["parent_view_id"],
            "base_fingerprint": row["base_fingerprint"],
            "patch_id": row["patch_id"],
            "status": row["status"],
        }

    def get_view(self, view_id: int) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM rule_views WHERE view_id = ?", (view_id,)
            ).fetchone()
            return self._view_row_dict(row) if row else None
        finally:
            connection.close()

    def _snapshot_chain_fingerprint(
        self, connection: sqlite3.Connection, active: sqlite3.Row
    ) -> tuple[int, str]:
        row = active
        seen: set[int] = set()
        while row["kind"] == "patch":
            if row["view_id"] in seen:
                raise invalid("E_CORRUPT_STATE", "patch parent chain contains a cycle")
            seen.add(row["view_id"])
            if row["parent_view_id"] is None:
                raise invalid("E_CORRUPT_STATE", "patch view has no parent")
            row = connection.execute(
                "SELECT * FROM rule_views WHERE view_id = ?", (row["parent_view_id"],)
            ).fetchone()
            if row is None:
                raise invalid("E_CORRUPT_STATE", "patch parent view does not exist")
        return row["view_id"], row["fingerprint"]

    def activate_snapshot(
        self,
        merged: MergedSnapshot,
        topology: Topology,
        *,
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> dict[str, Any]:
        with self.locked():
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                active = self._active_view_row(connection)
                if active is not None:
                    base_view_id, base_fingerprint = self._snapshot_chain_fingerprint(
                        connection, active
                    )
                    if base_fingerprint == merged.fingerprint:
                        connection.execute("ROLLBACK")
                        return {
                            "view_id": active["view_id"],
                            "result_revision": self.get_view_result_revision(
                                active["view_id"], connection=connection
                            ),
                            "snapshot_version": active["snapshot_version"],
                            "created": False,
                            "reason": "identical_snapshot_already_active",
                        }
                    if merged.snapshot_version < active["snapshot_version"]:
                        raise invalid(
                            "E_VIEW_ROLLBACK",
                            "snapshot version would move the active view backwards",
                            incoming_snapshot_version=merged.snapshot_version,
                            active_snapshot_version=active["snapshot_version"],
                        )
                    if merged.snapshot_version == active["snapshot_version"]:
                        base_row = connection.execute(
                            "SELECT * FROM rule_views WHERE view_id = ?", (base_view_id,)
                        ).fetchone()
                        if (
                            base_row is None
                            or base_row["source_fingerprint"]
                            != merged.source_fingerprint
                        ):
                            raise invalid(
                                "E_SNAPSHOT_VERSION_CONFLICT",
                                "same snapshot version has different source content",
                                snapshot_version=merged.snapshot_version,
                                active_source_fingerprint=(
                                    base_row["source_fingerprint"] if base_row else None
                                ),
                                incoming_source_fingerprint=merged.source_fingerprint,
                            )

                view_id = self._new_view_id(connection)
                result_revision = self._allocate_result_revision(connection)
                connection.execute(
                    """
                    INSERT INTO rule_views(
                        view_id, kind, snapshot_version, logical_clock_ms,
                        fingerprint, source_fingerprint,
                        parent_view_id, base_fingerprint,
                        patch_id, status, source_json
                    ) VALUES (?, 'snapshot', ?, ?, ?, ?, NULL, NULL, NULL, 'active', ?)
                    """,
                    (
                        view_id,
                        merged.snapshot_version,
                        merged.logical_clock_ms,
                        merged.fingerprint,
                        merged.source_fingerprint,
                        canonical_json(
                            {
                                "injected_clock_ms": merged.injected_clock_ms,
                                "sources": list(merged.sources),
                            }
                        ),
                    ),
                )
                connection.execute(
                    "CREATE TEMP TABLE IF NOT EXISTS snapshot_rule_stage "
                    "(record_json TEXT NOT NULL, rule_name TEXT NOT NULL)"
                )
                connection.execute("DELETE FROM snapshot_rule_stage")
                for rule in merged.rules:
                    connection.execute(
                        "INSERT INTO snapshot_rule_stage(record_json, rule_name) VALUES (?, ?)",
                        (canonical_json(rule.to_record()), rule.rule_name),
                    )
                connection.execute(
                    """
                    INSERT INTO rules(
                        view_id, rule_name, service, severity, effective_at_ms,
                        tombstone, source_version, generation, record_json
                    )
                    SELECT ?, rule_name,
                           json_extract(record_json, '$.service'),
                           json_extract(record_json, '$.severity'),
                           json_extract(record_json, '$.effective_at_ms'),
                           json_extract(record_json, '$.tombstone'),
                           json_extract(record_json, '$.source_version'),
                           json_extract(record_json, '$.generation'),
                           record_json
                      FROM snapshot_rule_stage
                    """,
                    (view_id,),
                )
                for migration in merged.migrations:
                    self._store_migration(connection, migration)
                connection.execute(
                    """
                    INSERT INTO view_results(view_id, result_revision, completed)
                    VALUES (?, ?, 0)
                    """,
                    (view_id, result_revision),
                )
                connection.execute(
                    """
                    INSERT INTO topology_snapshots(fingerprint, document_json, loaded_clock_ms)
                    VALUES (?, ?, ?)
                    ON CONFLICT(fingerprint) DO UPDATE SET
                        document_json = excluded.document_json,
                        loaded_clock_ms = excluded.loaded_clock_ms
                    """,
                    (
                        topology.fingerprint,
                        canonical_json(topology.document),
                        merged.injected_clock_ms,
                    ),
                )
                self._set_meta(connection, "topology_fingerprint", topology.fingerprint)
                clock_watermark = max(
                    int(self._meta(connection, "logical_clock_watermark_ms") or 0),
                    merged.logical_clock_ms,
                    merged.injected_clock_ms,
                )
                self._set_meta(
                    connection, "logical_clock_watermark_ms", clock_watermark
                )
                connection.execute(
                    "UPDATE rule_views SET status = 'superseded' WHERE status = 'active'"
                )
                connection.execute(
                    "UPDATE rule_views SET status = 'active' WHERE view_id = ?",
                    (view_id,),
                )
                connection.execute(
                    """
                    INSERT INTO view_pointer(singleton, view_id) VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET view_id = excluded.view_id
                    """,
                    (view_id,),
                )
                if crash_hook:
                    crash_hook("snapshot_before_commit", str(view_id))
                connection.execute("COMMIT")
                return {
                    "view_id": view_id,
                    "result_revision": result_revision,
                    "snapshot_version": merged.snapshot_version,
                    "created": True,
                    "reason": "snapshot_activated",
                }
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def _store_migration(
        self, connection: sqlite3.Connection, migration: dict[str, Any]
    ) -> None:
        key = migration_decision_key(migration)
        decision_json = migration_decision_json(migration)
        existing = connection.execute(
            "SELECT decision_json FROM migration_decisions WHERE decision_key = ?",
            (key,),
        ).fetchone()
        if existing is not None:
            if existing["decision_json"] != decision_json:
                raise invalid(
                    "E_NONDETERMINISTIC",
                    "migration decision changed for the same logical clock",
                    decision_key=key,
                )
            return
        connection.execute(
            """
            INSERT INTO migration_decisions(
                decision_key, source_version, rule_name, generation,
                source_clock_ms, injected_clock_ms, effective_at_ms,
                scale_ms_per_unit, rounding_direction, decision_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                migration["source_version"],
                migration["rule_name"],
                migration["generation"],
                migration["source_clock_ms"],
                migration["injected_clock_ms"],
                migration["effective_at_ms"],
                migration["scale_ms_per_unit"],
                migration["rounding_direction"],
                decision_json,
            ),
        )

    def get_view_result_revision(
        self,
        view_id: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> int:
        owns = connection is None
        if owns:
            connection = self.connect()
        try:
            row = connection.execute(
                "SELECT result_revision FROM view_results WHERE view_id = ?", (view_id,)
            ).fetchone()
            if row is None:
                raise invalid("E_CORRUPT_STATE", "view has no result revision", view_id=view_id)
            return int(row["result_revision"])
        finally:
            if owns:
                connection.close()

    def get_rules(self, view_id: int | None = None) -> dict[str, dict[str, Any]]:
        connection = self.connect()
        try:
            if view_id is None:
                active = self._active_view_row(connection)
                if active is None:
                    return {}
                view_id = active["view_id"]
            rows = connection.execute(
                "SELECT record_json FROM rules WHERE view_id = ? ORDER BY rule_name",
                (view_id,),
            )
            return {json.loads(row["record_json"])["rule_name"]: json.loads(row["record_json"]) for row in rows}
        finally:
            connection.close()

    def get_topology(self, fingerprint: str | None = None) -> Topology:
        from .topology import topology_from_document

        connection = self.connect()
        try:
            if fingerprint is None:
                fingerprint = self._meta(connection, "topology_fingerprint")
            if not fingerprint:
                raise invalid("E_TOPOLOGY_NOT_LOADED", "no topology has been loaded")
            row = connection.execute(
                "SELECT document_json FROM topology_snapshots WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            if row is None:
                raise invalid("E_TOPOLOGY_NOT_LOADED", "active topology is missing")
            document = json.loads(row["document_json"])
            return topology_from_document(document, fingerprint)
        finally:
            connection.close()

    def get_meta_state(self) -> dict[str, Any]:
        connection = self.connect()
        try:
            meta = {
                row["key"]: row["value"]
                for row in connection.execute("SELECT key, value FROM meta")
            }
            result = {
                "event_watermark_ms": int(meta.get("event_watermark_ms", "0")),
                "logical_clock_watermark_ms": int(
                    meta.get("logical_clock_watermark_ms", "0")
                ),
                "next_result_revision": int(meta.get("next_result_revision", "0")),
                "topology_fingerprint": meta.get("topology_fingerprint"),
            }
            return result
        finally:
            connection.close()

    @staticmethod
    def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_uid": row["event_uid"],
            "stream_id": row["stream_id"],
            "seq": row["seq"],
            "instance_id": row["instance_id"],
            "rule_name": row["rule_name"],
            "severity": row["severity"],
            "event_ts_ms": row["event_ts_ms"],
            "metric_value": json.loads(row["metric_value_json"]),
            "labels": json.loads(row["labels_json"]),
            "content_hash": row["content_hash"],
            "late": row["late"],
            "status": row["status"],
            "signal_eligible": row["signal_eligible"],
        }

    def get_events(self, stream_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            if stream_id is None:
                rows = connection.execute(
                    "SELECT * FROM events ORDER BY stream_id, seq"
                )
            else:
                rows = connection.execute(
                    "SELECT * FROM events WHERE stream_id = ? ORDER BY seq", (stream_id,)
                )
            return [self._event_row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def stream_materialization(self, stream: EventStream) -> tuple[bool, str | None]:
        connection = self.connect()
        try:
            rows = {
                row["seq"]: row
                for row in connection.execute(
                    "SELECT seq, content_hash FROM events WHERE stream_id = ?",
                    (stream.stream_id,),
                )
            }
            for seed in stream.events:
                row = rows.get(seed.seq)
                if row is None:
                    return False, None
                if row["content_hash"] != seed.content_hash:
                    return False, "content_conflict"
            return True, None
        finally:
            connection.close()

    def get_view_windows(self, view_id: int) -> list[int]:
        connection = self.connect()
        try:
            return sorted(
                {
                    int(row["window_ms"])
                    for row in connection.execute(
                        "SELECT window_ms FROM ingest_runs WHERE view_id = ?",
                        (view_id,),
                    )
                }
            )
        finally:
            connection.close()

    def run_id_for(self, view_id: int, stream: EventStream) -> str:
        return sha256_json(
            {
                "kind": "ingest",
                "view_id": view_id,
                "stream_id": stream.stream_id,
                "stream_fingerprint": stream.fingerprint,
            }
        )

    def get_ingest_run(self, run_id: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM ingest_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._run_row_dict(row) if row else None
        finally:
            connection.close()

    @staticmethod
    def _run_row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": row["run_id"],
            "view_id": row["view_id"],
            "stream_id": row["stream_id"],
            "stream_hash": row["stream_hash"],
            "stream_json": json.loads(row["stream_json"]),
            "injected_clock_ms": row["injected_clock_ms"],
            "window_ms": row["window_ms"],
            "allowed_lateness_ms": row["allowed_lateness_ms"],
            "result_revision": row["result_revision"],
            "status": row["status"],
            "expected_cluster_ids": json.loads(row["expected_cluster_ids_json"]),
            "committed_clusters": row["committed_clusters"],
        }

    def list_incomplete_ingest_runs(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                "SELECT * FROM ingest_runs WHERE status != 'complete' ORDER BY run_id"
            )
            return [self._run_row_dict(row) for row in rows]
        finally:
            connection.close()

    def _closed_cluster_for_event(
        self,
        connection: sqlite3.Connection,
        result_revision: int,
        rule_name: str,
        service: str,
        event_ts_ms: int,
        window_ms: int,
    ) -> bool:
        rows = connection.execute(
            """
            SELECT first_event_ms, last_event_ms
              FROM clusters
             WHERE result_revision = ? AND rule_name = ? AND service = ?
            """,
            (result_revision, rule_name, service),
        )
        for row in rows:
            if (
                row["first_event_ms"] - window_ms
                <= event_ts_ms
                <= row["last_event_ms"] + window_ms
            ):
                return True
        return False

    def register_ingest(
        self,
        view_id: int,
        stream: EventStream,
        rules: dict[str, MergedRule],
        topology: Topology,
    ) -> dict[str, Any]:
        run_id = self.run_id_for(view_id, stream)
        with self.locked():
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                active = self._active_view_row(connection)
                if active is None or active["view_id"] != view_id:
                    raise invalid(
                        "E_VIEW_NOT_ACTIVE",
                        "event ingestion must target the active rule view",
                        target_view_id=view_id,
                        active_view_id=active["view_id"] if active else None,
                    )
                existing_run = connection.execute(
                    "SELECT * FROM ingest_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing_run is not None:
                    result = self._run_row_dict(existing_run)
                    connection.execute("ROLLBACK")
                    result["created"] = False
                    return result
                clock_watermark = int(
                    self._meta(connection, "logical_clock_watermark_ms") or 0
                )
                if stream.injected_clock_ms < clock_watermark:
                    raise invalid(
                        "E_CLOCK_ROLLBACK",
                        "injected logical clock would move backwards",
                        injected_clock_ms=stream.injected_clock_ms,
                        logical_clock_watermark_ms=clock_watermark,
                    )
                stream_row = connection.execute(
                    "SELECT * FROM streams WHERE stream_id = ?", (stream.stream_id,)
                ).fetchone()
                if stream_row is not None and (
                    stream_row["window_ms"] != stream.window_ms
                    or stream_row["allowed_lateness_ms"] != stream.allowed_lateness_ms
                ):
                    raise invalid(
                        "E_STREAM_CONFLICT",
                        "stream window configuration changed",
                        stream_id=stream.stream_id,
                    )
                if stream_row is None:
                    watermark = 0
                    connection.execute(
                        """
                        INSERT INTO streams(
                            stream_id, window_ms, allowed_lateness_ms,
                            injected_clock_ms, stream_hash, watermark_ms
                        ) VALUES (?, ?, ?, ?, ?, 0)
                        """,
                        (
                            stream.stream_id,
                            stream.window_ms,
                            stream.allowed_lateness_ms,
                            stream.injected_clock_ms,
                            stream.fingerprint,
                        ),
                    )
                else:
                    watermark = int(stream_row["watermark_ms"])
                result_revision = self.get_view_result_revision(
                    view_id, connection=connection
                )
                existing_by_uid = {
                    row["event_uid"]: row
                    for row in connection.execute(
                        "SELECT * FROM events WHERE stream_id = ?", (stream.stream_id,)
                    )
                }
                inserted = 0
                late_inserted = 0
                max_seen = watermark
                for seed in stream.events:
                    existing = existing_by_uid.get(seed.event_uid)
                    if existing is not None:
                        if existing["content_hash"] != seed.content_hash:
                            raise invalid(
                                "E_EVENT_CONFLICT",
                                "same stream sequence has different content",
                                stream_id=seed.stream_id,
                                seq=seed.seq,
                            )
                        continue
                    rule = rules.get(seed.rule_name)
                    instance_service = topology.instance_to_service[seed.instance_id]
                    if seed.event_ts_ms > stream.injected_clock_ms:
                        status = "future"
                        late = 0
                        signal_eligible = 0
                    elif rule is None:
                        status = "unknown_rule"
                        late = 0
                        signal_eligible = 0
                    elif rule.tombstone:
                        status = "tombstoned_rule"
                        late = 0
                        signal_eligible = 0
                    elif rule.service != instance_service or seed.instance_id not in rule.instances:
                        status = "instance_not_in_rule"
                        late = 0
                        signal_eligible = 0
                    else:
                        cutoff = max_seen - stream.allowed_lateness_ms
                        if max_seen and seed.event_ts_ms < cutoff:
                            status = "late_watermark"
                            late = 1
                            signal_eligible = 0
                        elif self._closed_cluster_for_event(
                            connection,
                            result_revision,
                            seed.rule_name,
                            rule.service,
                            seed.event_ts_ms,
                            stream.window_ms,
                        ):
                            status = "late_closed_cluster"
                            late = 1
                            signal_eligible = 0
                        elif seed.event_ts_ms < rule.effective_at_ms:
                            status = "before_effective"
                            late = 0
                            signal_eligible = 1
                            max_seen = max(max_seen, seed.event_ts_ms)
                        else:
                            status = "eligible"
                            late = 0
                            signal_eligible = 1
                            max_seen = max(max_seen, seed.event_ts_ms)
                    if rule is not None and not late and seed.event_ts_ms <= stream.injected_clock_ms:
                        max_seen = max(max_seen, seed.event_ts_ms)
                    connection.execute(
                        """
                        INSERT INTO events(
                            event_uid, stream_id, seq, instance_id, rule_name,
                            severity, event_ts_ms, metric_value_json, labels_json,
                            content_hash, late, status, signal_eligible,
                            first_seen_view_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            seed.event_uid,
                            seed.stream_id,
                            seed.seq,
                            seed.instance_id,
                            seed.rule_name,
                            seed.severity,
                            seed.event_ts_ms,
                            canonical_json(seed.metric_value),
                            canonical_json(seed.labels),
                            seed.content_hash,
                            late,
                            status,
                            signal_eligible,
                            view_id,
                        ),
                    )
                    inserted += 1
                    if late:
                        late_inserted += 1
                connection.execute(
                    """
                    UPDATE streams
                       SET injected_clock_ms = ?, stream_hash = ?, watermark_ms = ?
                     WHERE stream_id = ?
                    """,
                    (
                        max(stream.injected_clock_ms, watermark),
                        stream.fingerprint,
                        max_seen,
                        stream.stream_id,
                    ),
                )
                global_watermark = max(
                    int(self._meta(connection, "event_watermark_ms") or 0), max_seen
                )
                self._set_meta(connection, "event_watermark_ms", global_watermark)
                self._set_meta(
                    connection,
                    "logical_clock_watermark_ms",
                    max(clock_watermark, stream.injected_clock_ms),
                )
                connection.execute(
                    "UPDATE view_results SET completed = 0 WHERE view_id = ?", (view_id,)
                )
                connection.execute(
                    """
                    INSERT INTO ingest_runs(
                        run_id, view_id, stream_id, stream_hash, stream_json,
                        injected_clock_ms, window_ms, allowed_lateness_ms,
                        result_revision, status, expected_cluster_ids_json,
                        committed_clusters
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'started', '[]', 0)
                    """,
                    (
                        run_id,
                        view_id,
                        stream.stream_id,
                        stream.fingerprint,
                        canonical_json(stream.document),
                        stream.injected_clock_ms,
                        stream.window_ms,
                        stream.allowed_lateness_ms,
                        result_revision,
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "run_id": run_id,
                    "view_id": view_id,
                    "stream_id": stream.stream_id,
                    "result_revision": result_revision,
                    "injected_clock_ms": stream.injected_clock_ms,
                    "window_ms": stream.window_ms,
                    "allowed_lateness_ms": stream.allowed_lateness_ms,
                    "status": "started",
                    "expected_cluster_ids": [],
                    "committed_clusters": 0,
                    "inserted_event_count": inserted,
                    "late_event_count": late_inserted,
                    "created": True,
                }
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def set_expected_clusters(self, run_id: str, cluster_ids: list[str]) -> None:
        with self.locked():
            with self.transaction() as connection:
                connection.execute(
                    "UPDATE ingest_runs SET expected_cluster_ids_json = ? WHERE run_id = ?",
                    (canonical_json(sorted(cluster_ids)), run_id),
                )

    def get_clusters(self, result_revision: int) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            rows = connection.execute(
                """
                SELECT summary_json FROM clusters
                 WHERE result_revision = ?
                 ORDER BY service, rule_name, first_event_ms, cluster_id
                """,
                (result_revision,),
            )
            return [json.loads(row["summary_json"]) for row in rows]
        finally:
            connection.close()

    def get_candidates(
        self, result_revision: int, cluster_id: str | None = None
    ) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            if cluster_id is None:
                rows = connection.execute(
                    """
                    SELECT candidate_json FROM root_candidates
                     WHERE result_revision = ?
                     ORDER BY cluster_id, rank, candidate_id
                    """,
                    (result_revision,),
                )
            else:
                rows = connection.execute(
                    """
                    SELECT candidate_json FROM root_candidates
                     WHERE result_revision = ? AND cluster_id = ?
                     ORDER BY rank, candidate_id
                    """,
                    (result_revision, cluster_id),
                )
            return [json.loads(row["candidate_json"]) for row in rows]
        finally:
            connection.close()

    def commit_cluster(
        self,
        *,
        run_id: str,
        view_id: int,
        result_revision: int,
        cluster: dict[str, Any],
        candidates: list[dict[str, Any]],
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> bool:
        object_id = cluster["cluster_id"]
        payload_hash = sha256_json({"cluster": cluster, "candidates": candidates})
        with self.locked():
            connection = self.connect()
            try:
                existing = connection.execute(
                    """
                    SELECT summary_hash FROM clusters
                     WHERE result_revision = ? AND cluster_id = ?
                    """,
                    (result_revision, object_id),
                ).fetchone()
                if existing is not None:
                    return False
            finally:
                connection.close()
            journal = self.connect()
            try:
                journal.execute("BEGIN IMMEDIATE")
                row = journal.execute(
                    """
                    SELECT phase, payload_hash FROM commit_journal
                     WHERE run_id = ? AND object_type = 'cluster' AND object_id = ?
                    """,
                    (run_id, object_id),
                ).fetchone()
                if row is None:
                    journal.execute(
                        """
                        INSERT INTO commit_journal(
                            run_id, object_type, object_id, phase, payload_hash
                        ) VALUES (?, 'cluster', ?, 'started', ?)
                        """,
                        (run_id, object_id, payload_hash),
                    )
                elif row["payload_hash"] != payload_hash:
                    raise invalid(
                        "E_NONDETERMINISTIC",
                        "cluster payload changed while resuming",
                        cluster_id=object_id,
                    )
                journal.execute("COMMIT")
            except Exception:
                try:
                    journal.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                journal.close()
            if crash_hook:
                crash_hook("cluster_after_journal", object_id)
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    INSERT INTO clusters(
                        result_revision, cluster_id, view_id, rule_name, service,
                        first_event_ms, last_event_ms, occurrence_count,
                        suppressed_count, silence_suppressed_count,
                        summary_json, summary_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        result_revision,
                        object_id,
                        view_id,
                        cluster["rule_name"],
                        cluster["service"],
                        cluster["first_event_ms"],
                        cluster["last_event_ms"],
                        cluster["occurrence_count"],
                        cluster["suppressed_count"],
                        cluster["silence_suppressed_count"],
                        canonical_json(cluster),
                        sha256_json(cluster),
                    ),
                )
                for candidate in candidates:
                    connection.execute(
                        """
                        INSERT INTO root_candidates(
                            result_revision, cluster_id, candidate_id,
                            rank, candidate_json, candidate_hash
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            result_revision,
                            object_id,
                            candidate["candidate_id"],
                            candidate["rank"],
                            canonical_json(candidate),
                            sha256_json(candidate),
                        ),
                    )
                connection.execute(
                    """
                    UPDATE commit_journal SET phase = 'committed'
                     WHERE run_id = ? AND object_type = 'cluster' AND object_id = ?
                    """,
                    (run_id, object_id),
                )
                connection.execute(
                    """
                    UPDATE ingest_runs
                       SET committed_clusters = committed_clusters + 1
                     WHERE run_id = ?
                    """,
                    (run_id,),
                )
                if crash_hook:
                    crash_hook("cluster_before_commit", object_id)
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()
            if crash_hook:
                crash_hook("cluster_after_commit", object_id)
            return True

    def finalize_ingest(
        self,
        run_id: str,
        *,
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        with self.locked():
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                run = connection.execute(
                    "SELECT * FROM ingest_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise invalid("E_CORRUPT_STATE", "ingest run is missing", run_id=run_id)
                if run["status"] == "complete":
                    connection.execute("ROLLBACK")
                    return
                expected = json.loads(run["expected_cluster_ids_json"])
                committed = {
                    row["cluster_id"]
                    for row in connection.execute(
                        "SELECT cluster_id FROM clusters WHERE result_revision = ?",
                        (run["result_revision"],),
                    )
                }
                missing = sorted(set(expected) - committed)
                if missing:
                    raise invalid(
                        "E_INCOMPLETE_INGEST",
                        "cannot finalize an ingest before all clusters are committed",
                        missing_cluster_ids=missing,
                    )
                connection.execute(
                    "UPDATE ingest_runs SET status = 'complete' WHERE run_id = ?",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE view_results SET completed = 1 WHERE view_id = ?",
                    (run["view_id"],),
                )
                if crash_hook:
                    crash_hook("ingest_before_commit", run_id)
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def get_patch(self, patch_id: str) -> dict[str, Any] | None:
        connection = self.connect()
        try:
            row = connection.execute(
                "SELECT * FROM patches WHERE patch_id = ?", (patch_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "patch_id": row["patch_id"],
                "digest": row["digest"],
                "base_view_id": row["base_view_id"],
                "new_view_id": row["new_view_id"],
                "result_revision": row["result_revision"],
                "logical_clock_ms": row["logical_clock_ms"],
                "changes": json.loads(row["changes_json"]),
                "status": row["status"],
                "response": json.loads(row["response_json"])
                if row["response_json"]
                else None,
            }
        finally:
            connection.close()

    def list_incomplete_patches(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            ids = [
                row["patch_id"]
                for row in connection.execute(
                    "SELECT patch_id FROM patches WHERE status != 'complete' ORDER BY patch_id"
                )
            ]
        finally:
            connection.close()
        return [patch for patch_id in ids if (patch := self.get_patch(patch_id))]

    def create_patch_run(
        self,
        *,
        patch_id: str,
        digest: str,
        logical_clock_ms: int,
        changes: list[dict[str, Any]],
        changes_json: dict[str, Any],
        new_rules: list[MergedRule],
        base_view: dict[str, Any],
    ) -> dict[str, Any]:
        with self.locked():
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT * FROM patches WHERE patch_id = ?", (patch_id,)
                ).fetchone()
                if existing is not None:
                    if existing["digest"] != digest:
                        raise invalid(
                            "E_PATCH_CONFLICT",
                            "patch id was already used with different content",
                            patch_id=patch_id,
                        )
                    connection.execute("ROLLBACK")
                    return self.get_patch(patch_id)  # type: ignore[return-value]
                active = self._active_view_row(connection)
                if active is None or active["view_id"] != base_view["view_id"]:
                    raise invalid(
                        "E_VIEW_CONFLICT",
                        "patch base view is no longer active",
                        base_view_id=base_view["view_id"],
                    )
                clock_watermark = int(
                    self._meta(connection, "logical_clock_watermark_ms") or 0
                )
                if logical_clock_ms < clock_watermark:
                    raise invalid(
                        "E_CLOCK_ROLLBACK",
                        "patch clock would move logical time backwards",
                        logical_clock_ms=logical_clock_ms,
                        logical_clock_watermark_ms=clock_watermark,
                    )
                new_view_id = self._new_view_id(connection)
                result_revision = self._allocate_result_revision(connection)
                fingerprint = sha256_json(
                    {
                        "kind": "patch",
                        "base_fingerprint": base_view["fingerprint"],
                        "changes": changes_json,
                        "logical_clock_ms": logical_clock_ms,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO rule_views(
                        view_id, kind, snapshot_version, logical_clock_ms,
                        fingerprint, source_fingerprint,
                        parent_view_id, base_fingerprint,
                        patch_id, status, source_json
                    ) VALUES (?, 'patch', ?, ?, ?, ?, ?, ?, ?, 'staging', ?)
                    """,
                    (
                        new_view_id,
                        base_view["snapshot_version"],
                        logical_clock_ms,
                        fingerprint,
                        base_view["source_fingerprint"],
                        base_view["view_id"],
                        base_view["fingerprint"],
                        patch_id,
                        canonical_json(changes_json),
                    ),
                )
                for rule in new_rules:
                    record = rule.to_record()
                    connection.execute(
                        """
                        INSERT INTO rules(
                            view_id, rule_name, service, severity, effective_at_ms,
                            tombstone, source_version, generation, record_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_view_id,
                            rule.rule_name,
                            rule.service,
                            rule.severity,
                            rule.effective_at_ms,
                            1 if rule.tombstone else 0,
                            rule.source_version,
                            rule.generation,
                            canonical_json(record),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO view_results(view_id, result_revision, completed)
                    VALUES (?, ?, 0)
                    """,
                    (new_view_id, result_revision),
                )
                connection.execute(
                    """
                    INSERT INTO patches(
                        patch_id, digest, base_view_id, new_view_id,
                        result_revision, logical_clock_ms, changes_json,
                        status, response_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'prepared', NULL)
                    """,
                    (
                        patch_id,
                        digest,
                        base_view["view_id"],
                        new_view_id,
                        result_revision,
                        logical_clock_ms,
                        canonical_json(changes_json),
                    ),
                )
                connection.execute("COMMIT")
                return {
                    "patch_id": patch_id,
                    "digest": digest,
                    "base_view_id": base_view["view_id"],
                    "new_view_id": new_view_id,
                    "result_revision": result_revision,
                    "logical_clock_ms": logical_clock_ms,
                    "changes": changes,
                    "status": "prepared",
                    "response": None,
                    "created": True,
                }
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    def commit_patch_results(
        self,
        *,
        patch: dict[str, Any],
        clusters: list[dict[str, Any]],
        candidates_by_cluster: dict[str, list[dict[str, Any]]],
        invalidations: list[dict[str, Any]],
        response: dict[str, Any],
        crash_hook: Callable[[str, str], None] | None = None,
    ) -> None:
        patch_id = patch["patch_id"]
        with self.locked():
            journal = self.connect()
            try:
                journal.execute("BEGIN IMMEDIATE")
                journal.execute(
                    """
                    INSERT INTO commit_journal(
                        run_id, object_type, object_id, phase, payload_hash
                    ) VALUES (?, 'patch', ?, 'started', ?)
                    ON CONFLICT(run_id, object_type, object_id) DO NOTHING
                    """,
                    (
                        f"patch:{patch_id}",
                        str(patch["new_view_id"]),
                        sha256_json(response),
                    ),
                )
                journal.execute("COMMIT")
            except Exception:
                try:
                    journal.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                journal.close()
            if crash_hook:
                crash_hook("patch_after_journal", patch_id)
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT status FROM patches WHERE patch_id = ?", (patch_id,)
                ).fetchone()
                if row is None:
                    raise invalid("E_CORRUPT_STATE", "patch row is missing", patch_id=patch_id)
                if row["status"] == "complete":
                    connection.execute("ROLLBACK")
                    return
                view_id = patch["new_view_id"]
                result_revision = patch["result_revision"]
                for cluster in clusters:
                    candidates = candidates_by_cluster[cluster["cluster_id"]]
                    connection.execute(
                        """
                        INSERT INTO clusters(
                            result_revision, cluster_id, view_id, rule_name, service,
                            first_event_ms, last_event_ms, occurrence_count,
                            suppressed_count, silence_suppressed_count,
                            summary_json, summary_hash
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            result_revision,
                            cluster["cluster_id"],
                            view_id,
                            cluster["rule_name"],
                            cluster["service"],
                            cluster["first_event_ms"],
                            cluster["last_event_ms"],
                            cluster["occurrence_count"],
                            cluster["suppressed_count"],
                            cluster["silence_suppressed_count"],
                            canonical_json(cluster),
                            sha256_json(cluster),
                        ),
                    )
                    for candidate in candidates:
                        connection.execute(
                            """
                            INSERT INTO root_candidates(
                                result_revision, cluster_id, candidate_id,
                                rank, candidate_json, candidate_hash
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                result_revision,
                                cluster["cluster_id"],
                                candidate["candidate_id"],
                                candidate["rank"],
                                canonical_json(candidate),
                                sha256_json(candidate),
                            ),
                        )
                for item in invalidations:
                    connection.execute(
                        """
                        INSERT INTO invalidations(
                            patch_id, view_id, result_revision, trigger_source,
                            trigger_kind, trigger_rule, reason, object_type,
                            object_id, path, action, evidence_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            patch_id,
                            view_id,
                            result_revision,
                            item["trigger_source"],
                            item["trigger_kind"],
                            item["trigger_rule"],
                            item["reason"],
                            item["object_type"],
                            item["object_id"],
                            item["path"],
                            item["action"],
                            canonical_json(item.get("evidence", {})),
                        ),
                    )
                if crash_hook:
                    crash_hook("patch_before_commit", patch_id)
                old_view_id = patch["base_view_id"]
                connection.execute(
                    "UPDATE rule_views SET status = 'superseded' WHERE view_id = ?",
                    (old_view_id,),
                )
                connection.execute(
                    "UPDATE rule_views SET status = 'active' WHERE view_id = ?",
                    (view_id,),
                )
                connection.execute(
                    """
                    INSERT INTO view_pointer(singleton, view_id) VALUES (1, ?)
                    ON CONFLICT(singleton) DO UPDATE SET view_id = excluded.view_id
                    """,
                    (view_id,),
                )
                connection.execute(
                    "UPDATE view_results SET completed = 1 WHERE view_id = ?",
                    (view_id,),
                )
                connection.execute(
                    """
                    UPDATE patches
                       SET status = 'complete', response_json = ?
                     WHERE patch_id = ?
                    """,
                    (canonical_json(response), patch_id),
                )
                clock_watermark = max(
                    int(self._meta(connection, "logical_clock_watermark_ms") or 0),
                    patch["logical_clock_ms"],
                )
                self._set_meta(
                    connection, "logical_clock_watermark_ms", clock_watermark
                )
                connection.execute(
                    """
                    UPDATE commit_journal SET phase = 'committed'
                     WHERE run_id = ? AND object_type = 'patch' AND object_id = ?
                    """,
                    (f"patch:{patch_id}", str(view_id)),
                )
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()
            if crash_hook:
                crash_hook("patch_after_commit", patch_id)

    def get_invalidations(self, patch_id: str | None = None) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            if patch_id is None:
                rows = connection.execute(
                    """
                    SELECT * FROM invalidations
                     ORDER BY patch_id, object_type, object_id, path, action
                    """
                )
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM invalidations WHERE patch_id = ?
                     ORDER BY object_type, object_id, path, action
                    """,
                    (patch_id,),
                )
            return [
                {
                    "patch_id": row["patch_id"],
                    "view_id": row["view_id"],
                    "result_revision": row["result_revision"],
                    "trigger_source": row["trigger_source"],
                    "trigger_kind": row["trigger_kind"],
                    "trigger_rule": row["trigger_rule"],
                    "reason": row["reason"],
                    "object_type": row["object_type"],
                    "object_id": row["object_id"],
                    "path": row["path"],
                    "action": row["action"],
                    "evidence": json.loads(row["evidence_json"]),
                }
                for row in rows
            ]
        finally:
            connection.close()

    def get_patches(self) -> list[dict[str, Any]]:
        connection = self.connect()
        try:
            ids = [
                row["patch_id"]
                for row in connection.execute(
                    "SELECT patch_id FROM patches ORDER BY patch_id"
                )
            ]
        finally:
            connection.close()
        return [patch for patch_id in ids if (patch := self.get_patch(patch_id))]

    def status_counts(self, view_id: int, result_revision: int) -> dict[str, Any]:
        connection = self.connect()
        try:
            events = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN late = 1 THEN 1 ELSE 0 END) AS late,
                    SUM(CASE WHEN status = 'eligible' THEN 1 ELSE 0 END) AS eligible
                FROM events
                """
            ).fetchone()
            clusters = connection.execute(
                "SELECT COUNT(*) AS count FROM clusters WHERE result_revision = ?",
                (result_revision,),
            ).fetchone()
            candidates = connection.execute(
                "SELECT COUNT(*) AS count FROM root_candidates WHERE result_revision = ?",
                (result_revision,),
            ).fetchone()
            completed = connection.execute(
                "SELECT completed FROM view_results WHERE view_id = ?", (view_id,)
            ).fetchone()
            return {
                "event_count": int(events["total"] or 0),
                "late_event_count": int(events["late"] or 0),
                "eligible_event_count": int(events["eligible"] or 0),
                "cluster_count": int(clusters["count"] or 0),
                "candidate_count": int(candidates["count"] or 0),
                "result_completed": bool(completed["completed"]) if completed else False,
            }
        finally:
            connection.close()

    def get_cluster_hashes(self, result_revision: int) -> dict[str, str]:
        connection = self.connect()
        try:
            return {
                row["cluster_id"]: row["summary_hash"]
                for row in connection.execute(
                    "SELECT cluster_id, summary_hash FROM clusters WHERE result_revision = ?",
                    (result_revision,),
                )
            }
        finally:
            connection.close()

    def get_candidate_hashes(self, result_revision: int) -> dict[str, str]:
        connection = self.connect()
        try:
            return {
                f"{row['cluster_id']}/{row['candidate_id']}": row["candidate_hash"]
                for row in connection.execute(
                    """
                    SELECT cluster_id, candidate_id, candidate_hash
                      FROM root_candidates WHERE result_revision = ?
                    """,
                    (result_revision,),
                )
            }
        finally:
            connection.close()

    def reset(self) -> None:
        with self.locked():
            connection = self.connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                for table in (
                    "invalidations",
                    "commit_journal",
                    "root_candidates",
                    "clusters",
                    "ingest_runs",
                    "events",
                    "streams",
                    "patches",
                    "view_results",
                    "rules",
                    "migration_decisions",
                    "topology_snapshots",
                    "view_pointer",
                    "rule_views",
                    "meta",
                ):
                    connection.execute(f"DELETE FROM {table}")
                connection.execute("COMMIT")
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()
            self.initialize()
