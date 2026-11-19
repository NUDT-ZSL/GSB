"""SQLite persistence, version pointers, incremental invalidation, and recovery."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import canonical_json, digest
from .engine import (
    build_all_candidates,
    build_all_clusters,
    build_candidate_set,
    build_cluster_row,
    downstream_closure,
    recluster_groups,
)
from .errors import AlarmError, fail
from .parsing import (
    Event,
    Topology,
    Window,
    apply_topology_changes,
    parse_events,
    parse_patch,
    parse_topology,
)

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topology_versions (
    version INTEGER PRIMARY KEY,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS windows (
    window_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    service TEXT NOT NULL,
    instance_id TEXT,
    rule TEXT,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_version INTEGER NOT NULL,
    removed_version INTEGER,
    source_patch TEXT NOT NULL,
    digest TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_seq INTEGER PRIMARY KEY,
    instance_id TEXT NOT NULL,
    service TEXT NOT NULL,
    rule TEXT NOT NULL,
    severity TEXT NOT NULL,
    logical_ts INTEGER NOT NULL,
    metric_value REAL NOT NULL,
    silenced INTEGER NOT NULL,
    maintenance INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_group_idx ON events(service, rule, logical_ts, event_seq);
CREATE INDEX IF NOT EXISTS events_instance_idx ON events(instance_id, logical_ts, event_seq);

CREATE TABLE IF NOT EXISTS event_suppressions (
    event_seq INTEGER PRIMARY KEY,
    suppressed INTEGER NOT NULL,
    source TEXT NOT NULL,
    window_id TEXT,
    first_topology_version INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_batches (
    batch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    first_seq INTEGER NOT NULL,
    last_seq INTEGER NOT NULL,
    result_version INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS cluster_runs (
    result_version INTEGER PRIMARY KEY,
    topology_version INTEGER NOT NULL,
    event_watermark INTEGER NOT NULL,
    window_ms INTEGER NOT NULL,
    operation_id TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clusters (
    result_version INTEGER NOT NULL,
    cluster_key TEXT NOT NULL,
    service TEXT NOT NULL,
    rule TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    event_seqs_json TEXT NOT NULL,
    ordered_event_seqs_json TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL,
    suppressed_event_seqs_json TEXT NOT NULL,
    representative_event_seq INTEGER,
    representative_json TEXT,
    basis_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    computed_topology_version INTEGER NOT NULL,
    reused_from_version INTEGER,
    PRIMARY KEY (result_version, cluster_key)
);

CREATE TABLE IF NOT EXISTS candidates (
    result_version INTEGER NOT NULL,
    cluster_key TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    basis_text TEXT NOT NULL,
    computed_topology_version INTEGER NOT NULL,
    reused_from_version INTEGER,
    PRIMARY KEY (result_version, cluster_key)
);

CREATE TABLE IF NOT EXISTS patch_history (
    patch_id TEXT PRIMARY KEY,
    digest TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    target_version INTEGER NOT NULL,
    result_version INTEGER NOT NULL,
    summary_json TEXT NOT NULL,
    created_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invalidation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL,
    result_version INTEGER NOT NULL,
    topology_version INTEGER NOT NULL,
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    action TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS invalidation_operation_idx ON invalidation_log(operation_id, item_type, action);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT,
    created_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS stage_clusters (
    run_token TEXT NOT NULL,
    cluster_key TEXT NOT NULL,
    service TEXT NOT NULL,
    rule TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts INTEGER NOT NULL,
    event_seqs_json TEXT NOT NULL,
    ordered_event_seqs_json TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    suppressed_count INTEGER NOT NULL,
    suppressed_event_seqs_json TEXT NOT NULL,
    representative_event_seq INTEGER,
    representative_json TEXT,
    basis_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    computed_topology_version INTEGER NOT NULL,
    reused_from_version INTEGER,
    PRIMARY KEY (run_token, cluster_key)
);

CREATE TABLE IF NOT EXISTS stage_candidates (
    run_token TEXT NOT NULL,
    cluster_key TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    basis_text TEXT NOT NULL,
    computed_topology_version INTEGER NOT NULL,
    reused_from_version INTEGER,
    PRIMARY KEY (run_token, cluster_key)
);
"""


@dataclass
class Snapshot:
    topology_version: int
    topology: Topology
    result_version: int
    event_watermark: int
    window_ms: int
    active_windows: list[dict[str, Any]]
    events: list[dict[str, Any]]
    suppressed: set[int]
    clusters: dict[str, dict[str, Any]]
    candidates: dict[str, dict[str, Any]]


def _json(value: str) -> Any:
    return json.loads(value)


def _next_order(conn: sqlite3.Connection, table: str, column: str) -> int:
    row = conn.execute(f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}").fetchone()
    return int(row[0])


def _meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row[0])


def _set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, str(value)),
    )


def _event_row(event: Event, batch_id: str) -> tuple[Any, ...]:
    payload = event.payload()
    return (
        event.event_seq,
        event.instance_id,
        event.service,
        event.rule,
        event.severity,
        event.logical_ts,
        event.metric_value,
        int(event.silenced),
        int(event.maintenance),
        batch_id,
        digest(payload),
    )


def _window_from_row(row: sqlite3.Row) -> Window:
    return Window(
        window_id=row["window_id"],
        kind=row["kind"],
        service=row["service"],
        instance_id=row["instance_id"],
        rule=row["rule"],
        start_ts=row["start_ts"],
        end_ts=row["end_ts"],
    )


def _load_current_windows(conn: sqlite3.Connection, include_removed: bool = False) -> list[dict[str, Any]]:
    where = "" if include_removed else "WHERE status = 'active'"
    rows = conn.execute(
        f"SELECT * FROM windows {where} ORDER BY window_id"
    ).fetchall()
    return [dict(row) for row in rows]


def _load_events(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM events ORDER BY event_seq").fetchall()
    return [dict(row) for row in rows]


def _load_suppressed(conn: sqlite3.Connection) -> set[int]:
    rows = conn.execute(
        "SELECT event_seq FROM event_suppressions WHERE suppressed = 1 ORDER BY event_seq"
    ).fetchall()
    return {int(row[0]) for row in rows}


def _decode_cluster_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cluster_key": row["cluster_key"],
        "service": row["service"],
        "rule": row["rule"],
        "start_ts": row["start_ts"],
        "end_ts": row["end_ts"],
        "event_seqs": _json(row["event_seqs_json"]),
        "ordered_event_seqs": _json(row["ordered_event_seqs_json"]),
        "event_count": row["event_count"],
        "suppressed_count": row["suppressed_count"],
        "suppressed_event_seqs": _json(row["suppressed_event_seqs_json"]),
        "representative_event_seq": row["representative_event_seq"],
        "representative": _json(row["representative_json"]) if row["representative_json"] else None,
        "basis": _json(row["basis_json"]),
        "basis_json": row["basis_json"],
        "digest": row["digest"],
        "computed_topology_version": row["computed_topology_version"],
        "reused_from_version": row["reused_from_version"],
    }


def _decode_candidate_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "cluster_key": row["cluster_key"],
        "payload": _json(row["candidates_json"]),
        "candidates_json": row["candidates_json"],
        "digest": row["digest"],
        "basis_text": row["basis_text"],
        "computed_topology_version": row["computed_topology_version"],
        "reused_from_version": row["reused_from_version"],
    }


def _cluster_core(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "cluster_key",
            "service",
            "rule",
            "start_ts",
            "end_ts",
            "event_seqs",
            "ordered_event_seqs",
            "event_count",
            "suppressed_count",
            "suppressed_event_seqs",
            "representative_event_seq",
            "representative",
            "basis",
        )
    }


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.timeout = float(os.environ.get("ALARM_DEDUP_LOCK_TIMEOUT", "30"))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        return conn

    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        fail("DB_BUSY", "Timed out waiting for the database file lock")
                    time.sleep(0.02)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextmanager
    def _write_txn(self, conn: sqlite3.Connection) -> Iterator[None]:
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            fail("DB_BUSY", "Could not begin SQLite write transaction", reason=str(exc))
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            try:
                conn.commit()
            except sqlite3.OperationalError as exc:
                conn.rollback()
                fail("DB_BUSY", "Could not commit SQLite transaction", reason=str(exc))

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA_SQL)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")

    def _require_initialized(self, conn: sqlite3.Connection) -> None:
        if _meta(conn, "schema_version") is None:
            fail("STATE_NOT_INITIALIZED", "Run load before using this database")

    def _topology_at(self, conn: sqlite3.Connection, version: int) -> Topology:
        row = conn.execute(
            "SELECT payload_json FROM topology_versions WHERE version = ?",
            (version,),
        ).fetchone()
        if row is None:
            fail("STATE_INCONSISTENT", "Topology version is missing", version=version)
        return parse_topology(_json(row["payload_json"]))

    def _snapshot(self, conn: sqlite3.Connection) -> Snapshot:
        self._require_initialized(conn)
        topology_version = int(_meta(conn, "current_topology_version") or 0)
        result_version = int(_meta(conn, "current_result_version") or 0)
        event_watermark = int(_meta(conn, "event_watermark") or 0)
        window_ms = int(_meta(conn, "cluster_window_ms") or 0)
        topology = self._topology_at(conn, topology_version)
        windows = _load_current_windows(conn)
        events = _load_events(conn)
        suppressed = _load_suppressed(conn)
        cluster_rows = conn.execute(
            "SELECT * FROM clusters WHERE result_version = ? ORDER BY cluster_key",
            (result_version,),
        ).fetchall()
        candidate_rows = conn.execute(
            "SELECT * FROM candidates WHERE result_version = ? ORDER BY cluster_key",
            (result_version,),
        ).fetchall()
        clusters = {row["cluster_key"]: _decode_cluster_row(row) for row in cluster_rows}
        candidates = {row["cluster_key"]: _decode_candidate_row(row) for row in candidate_rows}
        return Snapshot(
            topology_version,
            topology,
            result_version,
            event_watermark,
            window_ms,
            windows,
            events,
            suppressed,
            clusters,
            candidates,
        )

    def initialize(
        self,
        topology: Topology,
        initial_windows: list[Window],
        window_ms: int,
    ) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._file_lock():
            conn = self._connect()
            try:
                self._ensure_schema(conn)
                with self._write_txn(conn):
                    current = _meta(conn, "current_topology_version")
                    if current is not None:
                        current_version = int(current)
                        row = conn.execute(
                            "SELECT digest FROM topology_versions WHERE version = ?",
                            (current_version,),
                        ).fetchone()
                        existing_window_ms = int(_meta(conn, "cluster_window_ms") or 0)
                        existing_windows = sorted(
                            (
                                {
                                    "window_id": item["window_id"],
                                    "kind": item["kind"],
                                    "service": item["service"],
                                    "instance_id": item["instance_id"],
                                    "rule": item["rule"],
                                    "start_ts": item["start_ts"],
                                    "end_ts": item["end_ts"],
                                }
                                for item in _load_current_windows(conn)
                            ),
                            key=lambda item: item["window_id"],
                        )
                        requested_windows = sorted(
                            (window.payload() for window in initial_windows),
                            key=lambda item: item["window_id"],
                        )
                        if (
                            current_version == topology.version
                            and row is not None
                            and row["digest"] == topology.digest
                            and existing_window_ms == window_ms
                            and canonical_json(existing_windows) == canonical_json(requested_windows)
                        ):
                            result = {
                                "ok": True,
                                "operation": "load",
                                "idempotent": True,
                                "topology_version": current_version,
                                "topology_digest": topology.digest,
                                "result_version": int(_meta(conn, "current_result_version") or 0),
                                "window_ms": window_ms,
                            }
                            self._maybe_crash("before_commit")
                            return result
                        if topology.version < current_version:
                            fail(
                                "PATCH_TOPOLOGY_VERSION_ROLLBACK",
                                "Initial topology version is older than persisted state",
                                current_version=current_version,
                                target_version=topology.version,
                            )
                        fail(
                            "STATE_TOPOLOGY_LOAD_CONFLICT",
                            "Database already contains a different topology",
                            current_version=current_version,
                            target_version=topology.version,
                        )

                    _set_meta(conn, "schema_version", SCHEMA_VERSION)
                    _set_meta(conn, "current_topology_version", topology.version)
                    _set_meta(conn, "current_result_version", 1)
                    _set_meta(conn, "event_watermark", 0)
                    _set_meta(conn, "cluster_window_ms", window_ms)
                    conn.execute(
                        "INSERT INTO topology_versions(version, digest, payload_json, created_order) "
                        "VALUES(?, ?, ?, 1)",
                        (topology.version, topology.digest, canonical_json(topology.payload)),
                    )
                    for window in sorted(initial_windows, key=lambda item: item.window_id):
                        conn.execute(
                            "INSERT INTO windows(window_id, kind, service, instance_id, rule, start_ts, "
                            "end_ts, status, created_version, removed_version, source_patch, digest) "
                            "VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, NULL, 'initial', ?)",
                            (
                                window.window_id,
                                window.kind,
                                window.service,
                                window.instance_id,
                                window.rule,
                                window.start_ts,
                                window.end_ts,
                                topology.version,
                                digest(window.payload()),
                            ),
                        )
                    summary = {
                        "operation": "load",
                        "topology_version": topology.version,
                        "cluster_count": 0,
                        "candidate_count": 0,
                    }
                    conn.execute(
                        "INSERT INTO cluster_runs(result_version, topology_version, event_watermark, "
                        "window_ms, operation_id, summary_json, created_order) "
                        "VALUES(1, ?, 0, ?, 'load', ?, 1)",
                        (topology.version, window_ms, canonical_json(summary)),
                    )
                    result = {
                        "ok": True,
                        "operation": "load",
                        "idempotent": False,
                        "topology_version": topology.version,
                        "topology_digest": topology.digest,
                        "result_version": 1,
                        "window_ms": window_ms,
                    }
                    self._maybe_crash("before_commit")
                    return result
            finally:
                conn.close()

    def prepare_ingest(
        self,
        payload: Any,
        requested_window_ms: int | None,
    ) -> tuple[list[Event], int]:
        if not self.path.exists():
            fail("STATE_NOT_INITIALIZED", "Run load before ingest")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            snapshot = self._snapshot(conn)
            events = parse_events(payload, snapshot.topology)
            window_ms = snapshot.window_ms
            if requested_window_ms is None and isinstance(payload, dict):
                requested_window_ms = payload.get("cluster_window_ms")
            if requested_window_ms is not None and requested_window_ms != window_ms:
                fail(
                    "CLUSTER_WINDOW_MISMATCH",
                    "Requested cluster window differs from persisted configuration",
                    persisted_window_ms=window_ms,
                    requested_window_ms=requested_window_ms,
                )
            conn.commit()
            return events, window_ms
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def prepare_patch(self, payload: Any) -> dict[str, Any]:
        if not self.path.exists():
            fail("STATE_NOT_INITIALIZED", "Run load before patch")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            snapshot = self._snapshot(conn)
            patch = parse_patch(payload, snapshot.topology)
            existing = conn.execute(
                "SELECT * FROM patch_history WHERE patch_id = ?",
                (patch["patch_id"],),
            ).fetchone()
            if existing is not None:
                if existing["digest"] != patch["digest"]:
                    fail(
                        "PATCH_ID_CONFLICT",
                        "Patch ID already exists with different content",
                        patch_id=patch["patch_id"],
                    )
                return patch
            if patch["topology_version"] < snapshot.topology_version:
                fail(
                    "PATCH_TOPOLOGY_VERSION_ROLLBACK",
                    "Patch target version is behind current topology",
                    current_version=snapshot.topology_version,
                    target_version=patch["topology_version"],
                )
            if patch["topology_version"] == snapshot.topology_version:
                fail(
                    "PATCH_VERSION_CONFLICT",
                    "Patch target version equals current topology with different content",
                    current_version=snapshot.topology_version,
                    target_version=patch["topology_version"],
                )
            if patch["base_topology_version"] > snapshot.topology_version:
                fail(
                    "PATCH_BASE_VERSION_AHEAD",
                    "Patch base version is ahead of current topology",
                    current_version=snapshot.topology_version,
                    base_version=patch["base_topology_version"],
                )
            if patch["base_topology_version"] != snapshot.topology_version:
                fail(
                    "PATCH_BASE_MISMATCH",
                    "Patch base version does not match current topology",
                    current_version=snapshot.topology_version,
                    base_version=patch["base_topology_version"],
                )
            if patch["topology_version"] <= patch["base_topology_version"]:
                fail(
                    "PATCH_VERSION_INVALID",
                    "Patch target version must be greater than base version",
                    base_version=patch["base_topology_version"],
                    target_version=patch["topology_version"],
                )
            apply_topology_changes(
                snapshot.topology,
                patch["changes"],
                patch["topology_version"],
            )
            existing_window_ids = {
                row["window_id"]
                for row in conn.execute("SELECT window_id FROM windows").fetchall()
            }
            for change in patch["changes"]:
                if (
                    change["type"] == "silence"
                    and change["action"] == "remove"
                    and change["window_id"] not in existing_window_ids
                ):
                    fail(
                        "PATCH_UNKNOWN_WINDOW",
                        "Patch references an undeclared window",
                        window_id=change["window_id"],
                    )
            return patch
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def submit_ingest(self, events: list[Event], window_ms: int) -> dict[str, Any]:
        payload = {
            "events": [event.payload() for event in events],
            "window_ms": window_ms,
        }
        return self._submit_job("ingest", payload)

    def submit_patch(self, patch: dict[str, Any]) -> dict[str, Any]:
        payload = {"patch": patch}
        return self._submit_job("patch", payload)

    def _submit_job(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        payload_json = canonical_json(payload)
        payload_digest = digest(payload)
        job_id = f"{kind}:{payload_digest}"
        with self._file_lock():
            conn = self._connect()
            try:
                self._require_initialized(conn)
                existing = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if existing is None:
                    with self._write_txn(conn):
                        created_order = _next_order(conn, "jobs", "created_order")
                        conn.execute(
                            "INSERT INTO jobs(job_id, kind, payload_digest, payload_json, status, "
                            "result_json, created_order) VALUES(?, ?, ?, ?, 'pending', NULL, ?)",
                            (job_id, kind, payload_digest, payload_json, created_order),
                        )
                    self._maybe_crash("job_enqueued")
                    existing = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                if existing["status"] == "done":
                    result = _json(existing["result_json"])
                    result["idempotent"] = True
                    return result
                if existing["status"] == "failed":
                    error = _json(existing["result_json"])
                    fail(
                        "JOB_FAILED",
                        "Persisted operation previously failed",
                        job_id=job_id,
                        original_error=error,
                    )
                try:
                    result = self._apply_job(conn, job_id, kind, payload)
                    return result
                except AlarmError as exc:
                    with self._write_txn(conn):
                        conn.execute(
                            "UPDATE jobs SET status = 'failed', result_json = ? WHERE job_id = ?",
                            (canonical_json(exc.to_dict()), job_id),
                        )
                    raise
            finally:
                conn.close()

    def _apply_job(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        kind: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if kind == "ingest":
            result = self._apply_ingest(conn, job_id, payload)
        elif kind == "patch":
            result = self._apply_patch(conn, job_id, payload["patch"])
        else:
            fail("INTERNAL_ERROR", "Unknown job kind", kind=kind)
        return result

    def _apply_ingest(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._write_txn(conn):
            snapshot = self._snapshot(conn)
            if int(payload["window_ms"]) != snapshot.window_ms:
                fail(
                    "CLUSTER_WINDOW_MISMATCH",
                    "Persisted cluster window changed before ingest",
                    persisted_window_ms=snapshot.window_ms,
                    requested_window_ms=int(payload["window_ms"]),
                )
            events = parse_events({"events": payload["events"]}, snapshot.topology)
            batch_payload = {"events": [event.payload() for event in events]}
            batch_id = digest(batch_payload)
            prior_batch = conn.execute(
                "SELECT * FROM event_batches WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            if prior_batch is not None:
                result = _json(prior_batch["response_json"])
                result["idempotent"] = True
                conn.execute(
                    "UPDATE jobs SET status = 'done', result_json = ? WHERE job_id = ?",
                    (canonical_json(result), job_id),
                )
                self._maybe_crash("before_commit")
                return result

            existing_rows: dict[int, dict[str, Any]] = {}
            if events:
                existing_rows = {
                    int(row["event_seq"]): dict(row)
                    for row in conn.execute(
                        "SELECT * FROM events WHERE event_seq BETWEEN ? AND ?",
                        (
                            min(event.event_seq for event in events),
                            max(event.event_seq for event in events),
                        ),
                    ).fetchall()
                }
            duplicate_count = 0
            new_events: list[Event] = []
            for event in events:
                existing = existing_rows.get(event.event_seq)
                if existing is None:
                    if event.event_seq <= snapshot.event_watermark:
                        fail(
                            "EVENT_SEQUENCE_NOT_STRICT",
                            "New event sequence is below the committed watermark",
                            event_seq=event.event_seq,
                            watermark=snapshot.event_watermark,
                        )
                    new_events.append(event)
                    continue
                expected_digest = digest(event.payload())
                if existing["digest"] != expected_digest:
                    fail(
                        "EVENT_SEQUENCE_CONFLICT",
                        "Existing event sequence has different content",
                        event_seq=event.event_seq,
                    )
                duplicate_count += 1

            if not new_events:
                result = {
                    "ok": True,
                    "operation": "ingest",
                    "batch_id": batch_id,
                    "accepted_events": 0,
                    "duplicate_events": duplicate_count,
                    "result_version": snapshot.result_version,
                    "topology_version": snapshot.topology_version,
                    "event_watermark": snapshot.event_watermark,
                    "cluster_count": len(snapshot.clusters),
                }
                conn.execute(
                    "INSERT INTO event_batches(batch_id, digest, first_seq, last_seq, result_version, "
                    "response_json, created_order) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        digest(batch_payload),
                        min((event.event_seq for event in events), default=0),
                        max((event.event_seq for event in events), default=0),
                        snapshot.result_version,
                        canonical_json(result),
                        _next_order(conn, "event_batches", "created_order"),
                    ),
                )
                conn.execute(
                    "UPDATE jobs SET status = 'done', result_json = ? WHERE job_id = ?",
                    (canonical_json(result), job_id),
                )
                self._maybe_crash("before_commit")
                return result

            active_windows = snapshot.active_windows
            for event in new_events:
                conn.execute(
                    "INSERT INTO events(event_seq, instance_id, service, rule, severity, logical_ts, "
                    "metric_value, silenced, maintenance, batch_id, digest) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    _event_row(event, batch_id),
                )
                suppression = self._initial_suppression(event.payload(), active_windows, snapshot.topology_version)
                conn.execute(
                    "INSERT INTO event_suppressions(event_seq, suppressed, source, window_id, "
                    "first_topology_version, detail_json) VALUES(?, ?, ?, ?, ?, ?)",
                    (
                        event.event_seq,
                        int(suppression["suppressed"]),
                        suppression["source"],
                        suppression["window_id"],
                        snapshot.topology_version,
                        canonical_json(suppression),
                    ),
                )

            all_events = _load_events(conn)
            suppressed = _load_suppressed(conn)
            old_result_version = snapshot.result_version
            new_result_version = old_result_version + 1
            changed_groups = {(event.service, event.rule) for event in new_events}
            changed_clusters = recluster_groups(
                all_events,
                changed_groups,
                snapshot.window_ms,
                suppressed,
                snapshot.topology,
            )
            stage: dict[str, dict[str, Any]] = {}
            cluster_actions: dict[str, tuple[str, str]] = {}
            for key, old_cluster in snapshot.clusters.items():
                group = (old_cluster["service"], old_cluster["rule"])
                if group not in changed_groups:
                    stage[key] = {
                        **old_cluster,
                        "computed_topology_version": old_cluster["computed_topology_version"],
                        "reused_from_version": old_result_version,
                    }
                    cluster_actions[key] = ("reuse", "group_not_touched_by_ingest")
            for key, row in changed_clusters.items():
                old_cluster = snapshot.clusters.get(key)
                if old_cluster is not None and old_cluster["digest"] == row["digest"]:
                    stage[key] = {
                        **row,
                        "computed_topology_version": old_cluster["computed_topology_version"],
                        "reused_from_version": old_result_version,
                    }
                    cluster_actions[key] = ("reuse", "cluster_digest_unchanged")
                else:
                    stage[key] = {
                        **row,
                        "computed_topology_version": snapshot.topology_version,
                        "reused_from_version": None,
                    }
                    action = "add" if old_cluster is None else "recompute"
                    cluster_actions[key] = (action, "new_event_changed_cluster")
            for key in sorted(set(snapshot.clusters) - set(stage)):
                cluster_actions[key] = ("remove", "cluster_merged_or_replaced")

            changed_services = {event.service for event in new_events}
            candidate_target_services: set[str] = set()
            for service in changed_services:
                candidate_target_services.update(downstream_closure(snapshot.topology, service))
            cluster_by_event = {
                seq: cluster["cluster_key"]
                for cluster in stage.values()
                for seq in cluster["event_seqs"]
            }
            candidate_stage: dict[str, dict[str, Any]] = {}
            candidate_actions: dict[str, tuple[str, str]] = {}
            for key in sorted(stage):
                cluster = stage[key]
                old_candidate = snapshot.candidates.get(key)
                must_recompute = (
                    old_candidate is None
                    or cluster_actions[key][0] != "reuse"
                    or cluster["service"] in candidate_target_services
                )
                if must_recompute:
                    computed = build_candidate_set(
                        snapshot.topology,
                        cluster,
                        all_events,
                        suppressed,
                        cluster_by_event,
                    )
                    candidate_stage[key] = {
                        "cluster_key": key,
                        "payload": computed.payload,
                        "candidates_json": canonical_json(computed.payload),
                        "digest": computed.digest,
                        "basis_text": computed.basis_text,
                        "computed_topology_version": snapshot.topology_version,
                        "reused_from_version": None,
                    }
                    action = "add" if old_candidate is None else "recompute"
                    candidate_actions[key] = (action, "event_signal_or_cluster_changed")
                else:
                    candidate_stage[key] = {
                        **old_candidate,
                        "computed_topology_version": old_candidate["computed_topology_version"],
                        "reused_from_version": old_result_version,
                    }
                    candidate_actions[key] = ("reuse", "candidate_scope_unaffected")
            for key in sorted(set(snapshot.candidates) - set(candidate_stage)):
                candidate_actions[key] = ("remove", "cluster_merged_or_replaced")

            watermark = max(event["event_seq"] for event in all_events)
            result = {
                "ok": True,
                "operation": "ingest",
                "batch_id": batch_id,
                "accepted_events": len(new_events),
                "duplicate_events": duplicate_count,
                "result_version": new_result_version,
                "topology_version": snapshot.topology_version,
                "event_watermark": watermark,
                "cluster_count": len(stage),
            }
            self._finalize_result(
                conn=conn,
                job_id=job_id,
                operation_id=job_id,
                topology_version=snapshot.topology_version,
                event_watermark=watermark,
                window_ms=snapshot.window_ms,
                result_version=new_result_version,
                clusters=stage,
                candidates=candidate_stage,
                cluster_actions=cluster_actions,
                candidate_actions=candidate_actions,
                summary=result,
            )
            conn.execute(
                "INSERT INTO event_batches(batch_id, digest, first_seq, last_seq, result_version, "
                "response_json, created_order) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    batch_id,
                    digest(batch_payload),
                    min((event.event_seq for event in events), default=0),
                    max((event.event_seq for event in events), default=0),
                    new_result_version,
                    canonical_json(result),
                    _next_order(conn, "event_batches", "created_order"),
                ),
            )
            conn.execute(
                "UPDATE jobs SET status = 'done', result_json = ? WHERE job_id = ?",
                (canonical_json(result), job_id),
            )
            self._maybe_crash("before_commit")
            return result

    def _apply_patch(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        patch_payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._write_txn(conn):
            snapshot = self._snapshot(conn)
            patch = parse_patch(patch_payload, snapshot.topology)
            existing_patch = conn.execute(
                "SELECT * FROM patch_history WHERE patch_id = ?",
                (patch["patch_id"],),
            ).fetchone()
            if existing_patch is not None:
                if existing_patch["digest"] != patch["digest"]:
                    fail(
                        "PATCH_ID_CONFLICT",
                        "Patch ID already exists with different content",
                        patch_id=patch["patch_id"],
                    )
                result = _json(existing_patch["summary_json"])
                result["idempotent"] = True
                conn.execute(
                    "UPDATE jobs SET status = 'done', result_json = ? WHERE job_id = ?",
                    (canonical_json(result), job_id),
                )
                self._maybe_crash("before_commit")
                return result
            if patch["topology_version"] < snapshot.topology_version:
                fail(
                    "PATCH_TOPOLOGY_VERSION_ROLLBACK",
                    "Patch target version is behind current topology",
                    current_version=snapshot.topology_version,
                    target_version=patch["topology_version"],
                )
            if patch["topology_version"] == snapshot.topology_version:
                fail(
                    "PATCH_VERSION_CONFLICT",
                    "Patch target version equals current topology with different content",
                    current_version=snapshot.topology_version,
                    target_version=patch["topology_version"],
                )
            if patch["base_topology_version"] != snapshot.topology_version:
                fail(
                    "PATCH_BASE_MISMATCH",
                    "Patch base version does not match current topology",
                    current_version=snapshot.topology_version,
                    base_version=patch["base_topology_version"],
                )

            new_topology = apply_topology_changes(
                snapshot.topology,
                patch["changes"],
                patch["topology_version"],
            )
            window_changes: dict[str, tuple[Window | None, Window | None]] = {}
            dependency_services: set[str] = set()
            weight_services: set[str] = set()
            for change in patch["changes"]:
                if change["type"] == "dependency":
                    if (
                        snapshot.topology.services[change["service"]].upstream_dependencies
                        != new_topology.services[change["service"]].upstream_dependencies
                    ):
                        dependency_services.add(change["service"])
                elif change["type"] == "weight":
                    if (
                        snapshot.topology.instances[change["instance_id"]].weight
                        != new_topology.instances[change["instance_id"]].weight
                    ):
                        weight_services.add(snapshot.topology.instances[change["instance_id"]].service)
                else:
                    if change["action"] == "remove":
                        window_id = change["window_id"]
                        row = conn.execute(
                            "SELECT * FROM windows WHERE window_id = ?",
                            (window_id,),
                        ).fetchone()
                        if row is None:
                            fail(
                                "PATCH_UNKNOWN_WINDOW",
                                "Patch references an undeclared window",
                                window_id=window_id,
                            )
                        old_window = _window_from_row(row) if row["status"] == "active" else None
                        conn.execute(
                            "UPDATE windows SET status = 'removed', removed_version = ?, "
                            "source_patch = ? WHERE window_id = ?",
                            (patch["topology_version"], patch["patch_id"], window_id),
                        )
                        if old_window is not None:
                            window_changes[window_id] = (old_window, None)
                    else:
                        window_payload = change["window"]
                        new_window = Window(
                            window_payload["window_id"],
                            window_payload["kind"],
                            window_payload["service"],
                            window_payload["instance_id"],
                            window_payload["rule"],
                            window_payload["start_ts"],
                            window_payload["end_ts"],
                        )
                        row = conn.execute(
                            "SELECT * FROM windows WHERE window_id = ?",
                            (new_window.window_id,),
                        ).fetchone()
                        old_window = _window_from_row(row) if row is not None and row["status"] == "active" else None
                        if row is None:
                            conn.execute(
                                "INSERT INTO windows(window_id, kind, service, instance_id, rule, "
                                "start_ts, end_ts, status, created_version, removed_version, "
                                "source_patch, digest) VALUES(?, ?, ?, ?, ?, ?, ?, 'active', ?, "
                                "NULL, ?, ?)",
                                (
                                    new_window.window_id,
                                    new_window.kind,
                                    new_window.service,
                                    new_window.instance_id,
                                    new_window.rule,
                                    new_window.start_ts,
                                    new_window.end_ts,
                                    patch["topology_version"],
                                    patch["patch_id"],
                                    digest(new_window.payload()),
                                ),
                            )
                        else:
                            conn.execute(
                                "UPDATE windows SET kind = ?, service = ?, instance_id = ?, rule = ?, "
                                "start_ts = ?, end_ts = ?, status = 'active', created_version = ?, "
                                "removed_version = NULL, source_patch = ?, digest = ? "
                                "WHERE window_id = ?",
                                (
                                    new_window.kind,
                                    new_window.service,
                                    new_window.instance_id,
                                    new_window.rule,
                                    new_window.start_ts,
                                    new_window.end_ts,
                                    patch["topology_version"],
                                    patch["patch_id"],
                                    digest(new_window.payload()),
                                    new_window.window_id,
                                ),
                            )
                        if old_window is None or old_window.payload() != new_window.payload():
                            window_changes[new_window.window_id] = (old_window, new_window)

            active_windows = _load_current_windows(conn)
            all_events = _load_events(conn)
            event_map = {event["event_seq"]: event for event in all_events}
            for event in all_events:
                suppression = self._initial_suppression(
                    event,
                    active_windows,
                    patch["topology_version"],
                )
                if not suppression["suppressed"]:
                    continue
                conn.execute(
                    "UPDATE event_suppressions SET suppressed = 1, source = ?, window_id = ?, "
                    "detail_json = ? WHERE event_seq = ? AND suppressed = 0",
                    (
                        suppression["source"],
                        suppression["window_id"],
                        canonical_json(suppression),
                        event["event_seq"],
                    ),
                )
            suppressed = _load_suppressed(conn)

            cluster_reasons: dict[str, set[str]] = {}
            for cluster in snapshot.clusters.values():
                seqs = set(cluster["event_seqs"])
                for instance_id in {
                    change["instance_id"]
                    for change in patch["changes"]
                    if change["type"] == "weight"
                }:
                    if any(event_map[seq]["instance_id"] == instance_id for seq in seqs):
                        cluster_reasons.setdefault(cluster["cluster_key"], set()).add(
                            f"weight_changed:{instance_id}"
                        )
                for window_id, (old_window, new_window) in window_changes.items():
                    if any(
                        (old_window is not None and old_window.covers(event_map[seq]))
                        or (new_window is not None and new_window.covers(event_map[seq]))
                        for seq in seqs
                    ):
                        cluster_reasons.setdefault(cluster["cluster_key"], set()).add(
                            f"window_changed:{window_id}"
                        )

            stage: dict[str, dict[str, Any]] = {}
            cluster_actions: dict[str, tuple[str, str]] = {}
            weights = {key: value.weight for key, value in new_topology.instances.items()}
            for key, old_cluster in snapshot.clusters.items():
                reasons = cluster_reasons.get(key)
                if reasons:
                    ordered = [
                        event_map[seq]
                        for seq in sorted(old_cluster["event_seqs"], key=lambda seq: (event_map[seq]["logical_ts"], seq))
                    ]
                    row = build_cluster_row(
                        old_cluster["service"],
                        old_cluster["rule"],
                        ordered,
                        suppressed,
                        weights,
                    )
                    stage[key] = {
                        **row,
                        "computed_topology_version": patch["topology_version"],
                        "reused_from_version": None,
                    }
                    cluster_actions[key] = ("recompute", ",".join(sorted(reasons)))
                else:
                    stage[key] = {
                        **old_cluster,
                        "computed_topology_version": old_cluster["computed_topology_version"],
                        "reused_from_version": snapshot.result_version,
                    }
                    cluster_actions[key] = ("reuse", "cluster_not_in_change_closure")

            changed_services = dependency_services | weight_services
            changed_services.update(
                window.service
                for old_window, new_window in window_changes.values()
                for window in (old_window, new_window)
                if window is not None
            )
            candidate_targets: set[str] = set()
            for service in changed_services:
                candidate_targets.update(downstream_closure(snapshot.topology, service))
                candidate_targets.update(downstream_closure(new_topology, service))
            cluster_by_event = {
                seq: cluster["cluster_key"]
                for cluster in stage.values()
                for seq in cluster["event_seqs"]
            }
            candidate_stage: dict[str, dict[str, Any]] = {}
            candidate_actions: dict[str, tuple[str, str]] = {}
            for key in sorted(stage):
                cluster = stage[key]
                old_candidate = snapshot.candidates.get(key)
                recompute = (
                    old_candidate is None
                    or cluster_actions[key][0] == "recompute"
                    or cluster["service"] in candidate_targets
                )
                if recompute:
                    computed = build_candidate_set(
                        new_topology,
                        cluster,
                        all_events,
                        suppressed,
                        cluster_by_event,
                    )
                    candidate_stage[key] = {
                        "cluster_key": key,
                        "payload": computed.payload,
                        "candidates_json": canonical_json(computed.payload),
                        "digest": computed.digest,
                        "basis_text": computed.basis_text,
                        "computed_topology_version": patch["topology_version"],
                        "reused_from_version": None,
                    }
                    action = "add" if old_candidate is None else "recompute"
                    candidate_actions[key] = (action, "service_in_rule_change_closure")
                else:
                    candidate_stage[key] = {
                        **old_candidate,
                        "computed_topology_version": old_candidate["computed_topology_version"],
                        "reused_from_version": snapshot.result_version,
                    }
                    candidate_actions[key] = ("reuse", "service_outside_rule_change_closure")

            new_result_version = snapshot.result_version + 1
            summary = {
                "ok": True,
                "operation": "patch",
                "patch_id": patch["patch_id"],
                "topology_version": patch["topology_version"],
                "base_topology_version": patch["base_topology_version"],
                "result_version": new_result_version,
                "event_watermark": snapshot.event_watermark,
                "cluster_count": len(stage),
                "changed_services": sorted(changed_services),
                "changed_silence_windows": sorted(window_changes),
            }
            self._finalize_result(
                conn=conn,
                job_id=job_id,
                operation_id=job_id,
                topology_version=patch["topology_version"],
                event_watermark=snapshot.event_watermark,
                window_ms=snapshot.window_ms,
                result_version=new_result_version,
                clusters=stage,
                candidates=candidate_stage,
                cluster_actions=cluster_actions,
                candidate_actions=candidate_actions,
                summary=summary,
            )
            conn.execute(
                "INSERT INTO topology_versions(version, digest, payload_json, created_order) "
                "VALUES(?, ?, ?, ?)",
                (
                    new_topology.version,
                    new_topology.digest,
                    canonical_json(new_topology.payload),
                    _next_order(conn, "topology_versions", "created_order"),
                ),
            )
            conn.execute(
                "INSERT INTO patch_history(patch_id, digest, base_version, target_version, "
                "result_version, summary_json, created_order) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    patch["patch_id"],
                    patch["digest"],
                    patch["base_topology_version"],
                    patch["topology_version"],
                    new_result_version,
                    canonical_json(summary),
                    _next_order(conn, "patch_history", "created_order"),
                ),
            )
            conn.execute(
                "UPDATE jobs SET status = 'done', result_json = ? WHERE job_id = ?",
                (canonical_json(summary), job_id),
            )
            self._maybe_crash("before_commit")
            return summary

    def _initial_suppression(
        self,
        event: dict[str, Any],
        active_windows: list[dict[str, Any]],
        topology_version: int,
    ) -> dict[str, Any]:
        if event["silenced"]:
            return {
                "suppressed": True,
                "source": "event_flag_silenced",
                "window_id": None,
                "topology_version": topology_version,
                "kind": None,
            }
        if event["maintenance"]:
            return {
                "suppressed": True,
                "source": "event_flag_maintenance",
                "window_id": None,
                "topology_version": topology_version,
                "kind": None,
            }
        windows: list[Window] = []
        for row in active_windows:
            windows.append(
                Window(
                    row["window_id"],
                    row["kind"],
                    row["service"],
                    row["instance_id"],
                    row["rule"],
                    row["start_ts"],
                    row["end_ts"],
                )
            )
        matching = sorted(
            (window for window in windows if window.covers(event)),
            key=lambda window: (window.start_ts, window.end_ts, window.window_id),
        )
        if matching:
            window = matching[0]
            return {
                "suppressed": True,
                "source": f"window_{window.kind}",
                "window_id": window.window_id,
                "topology_version": topology_version,
                "kind": window.kind,
            }
        return {
            "suppressed": False,
            "source": "none",
            "window_id": None,
            "topology_version": topology_version,
            "kind": None,
        }

    def _stage_rows(
        self,
        conn: sqlite3.Connection,
        run_token: str,
        clusters: dict[str, dict[str, Any]],
        candidates: dict[str, dict[str, Any]],
    ) -> None:
        conn.execute("DELETE FROM stage_clusters WHERE run_token = ?", (run_token,))
        conn.execute("DELETE FROM stage_candidates WHERE run_token = ?", (run_token,))
        for key in sorted(clusters):
            row = clusters[key]
            conn.execute(
                "INSERT INTO stage_clusters(run_token, cluster_key, service, rule, start_ts, end_ts, "
                "event_seqs_json, ordered_event_seqs_json, event_count, suppressed_count, "
                "suppressed_event_seqs_json, representative_event_seq, representative_json, "
                "basis_json, digest, computed_topology_version, reused_from_version) "
                "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run_token,
                    key,
                    row["service"],
                    row["rule"],
                    row["start_ts"],
                    row["end_ts"],
                    canonical_json(row["event_seqs"]),
                    canonical_json(row["ordered_event_seqs"]),
                    row["event_count"],
                    row["suppressed_count"],
                    canonical_json(row["suppressed_event_seqs"]),
                    row["representative_event_seq"],
                    canonical_json(row["representative"]) if row["representative"] is not None else None,
                    row["basis_json"],
                    row["digest"],
                    row["computed_topology_version"],
                    row["reused_from_version"],
                ),
            )
        for key in sorted(candidates):
            row = candidates[key]
            conn.execute(
                "INSERT INTO stage_candidates(run_token, cluster_key, candidates_json, digest, "
                "basis_text, computed_topology_version, reused_from_version) "
                "VALUES(?, ?, ?, ?, ?, ?, ?)",
                (
                    run_token,
                    key,
                    row["candidates_json"],
                    row["digest"],
                    row["basis_text"],
                    row["computed_topology_version"],
                    row["reused_from_version"],
                ),
            )

    def _finalize_result(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        operation_id: str,
        topology_version: int,
        event_watermark: int,
        window_ms: int,
        result_version: int,
        clusters: dict[str, dict[str, Any]],
        candidates: dict[str, dict[str, Any]],
        cluster_actions: dict[str, tuple[str, str]],
        candidate_actions: dict[str, tuple[str, str]],
        summary: dict[str, Any],
    ) -> None:
        run_token = job_id + ":" + str(result_version)
        self._stage_rows(conn, run_token, clusters, candidates)
        conn.execute(
            "INSERT INTO clusters(result_version, cluster_key, service, rule, start_ts, end_ts, "
            "event_seqs_json, ordered_event_seqs_json, event_count, suppressed_count, "
            "suppressed_event_seqs_json, representative_event_seq, representative_json, "
            "basis_json, digest, computed_topology_version, reused_from_version) "
            "SELECT ?, cluster_key, service, rule, start_ts, end_ts, event_seqs_json, "
            "ordered_event_seqs_json, event_count, suppressed_count, suppressed_event_seqs_json, "
            "representative_event_seq, representative_json, basis_json, digest, "
            "computed_topology_version, reused_from_version "
            "FROM stage_clusters WHERE run_token = ?",
            (result_version, run_token),
        )
        conn.execute(
            "INSERT INTO candidates(result_version, cluster_key, candidates_json, digest, "
            "basis_text, computed_topology_version, reused_from_version) "
            "SELECT ?, cluster_key, candidates_json, digest, basis_text, "
            "computed_topology_version, reused_from_version "
            "FROM stage_candidates WHERE run_token = ?",
            (result_version, run_token),
        )
        for item_type, actions in (("cluster", cluster_actions), ("candidate", candidate_actions)):
            for key in sorted(actions):
                action, reason = actions[key]
                details = {
                    "result_version": result_version,
                    "topology_version": topology_version,
                }
                conn.execute(
                    "INSERT INTO invalidation_log(operation_id, result_version, topology_version, "
                    "item_type, item_key, action, reason, details_json) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        operation_id,
                        result_version,
                        topology_version,
                        item_type,
                        key,
                        action,
                        reason,
                        canonical_json(details),
                    ),
                )
        conn.execute(
            "INSERT INTO cluster_runs(result_version, topology_version, event_watermark, "
            "window_ms, operation_id, summary_json, created_order) VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                result_version,
                topology_version,
                event_watermark,
                window_ms,
                operation_id,
                canonical_json(summary),
                _next_order(conn, "cluster_runs", "created_order"),
            ),
        )
        _set_meta(conn, "current_result_version", result_version)
        _set_meta(conn, "current_topology_version", topology_version)
        _set_meta(conn, "event_watermark", event_watermark)
        conn.execute("DELETE FROM stage_clusters WHERE run_token = ?", (run_token,))
        conn.execute("DELETE FROM stage_candidates WHERE run_token = ?", (run_token,))

    def status(self) -> dict[str, Any]:
        if not self.path.exists():
            fail("STATE_NOT_INITIALIZED", "State database does not exist")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            snapshot = self._snapshot(conn)
            active_windows = snapshot.active_windows
            candidate_rows = conn.execute(
                "SELECT * FROM candidates WHERE result_version = ? ORDER BY cluster_key",
                (snapshot.result_version,),
            ).fetchall()
            candidate_summaries = []
            for row in candidate_rows:
                payload = _json(row["candidates_json"])
                top = payload["candidates"][:3]
                cluster = snapshot.clusters.get(row["cluster_key"], {})
                candidate_summaries.append(
                    {
                        "cluster_key": row["cluster_key"],
                        "service": payload["target_service"],
                        "rule": cluster.get("rule"),
                        "representative_event_seq": cluster.get("representative_event_seq"),
                        "candidate_count": len(payload["candidates"]),
                        "top_candidates": [
                            {
                                "rank": item["rank"],
                                "candidate_instance_id": item["candidate_instance_id"],
                                "service": item["service"],
                                "causal_distance": item["causal_distance"],
                                "earliest_alert_ts": item["earliest_alert_ts"],
                                "basis_text": item["basis_text"],
                            }
                            for item in top
                        ],
                    }
                )
            run = conn.execute(
                "SELECT * FROM cluster_runs WHERE result_version = ?",
                (snapshot.result_version,),
            ).fetchone()
            operation_id = run["operation_id"] if run is not None else ""
            invalidation_rows = conn.execute(
                "SELECT item_type, action, COUNT(*) AS count FROM invalidation_log "
                "WHERE operation_id = ? GROUP BY item_type, action ORDER BY item_type, action",
                (operation_id,),
            ).fetchall()
            invalidation_counts: dict[str, int] = {}
            for row in invalidation_rows:
                invalidation_counts[f"{row['item_type']}_{row['action']}"] = int(row["count"])
            recompute_count = sum(
                count
                for key, count in invalidation_counts.items()
                if key.endswith("_recompute")
            )
            total_recompute = conn.execute(
                "SELECT COUNT(*) FROM invalidation_log WHERE action = 'recompute'"
            ).fetchone()[0]
            pending = conn.execute(
                "SELECT job_id, kind FROM jobs WHERE status = 'pending' ORDER BY created_order"
            ).fetchall()
            cluster_summaries = [
                {
                    "cluster_key": cluster["cluster_key"],
                    "service": cluster["service"],
                    "rule": cluster["rule"],
                    "event_count": cluster["event_count"],
                    "suppressed_count": cluster["suppressed_count"],
                    "representative_event_seq": cluster["representative_event_seq"],
                    "computed_topology_version": cluster["computed_topology_version"],
                    "reused_from_version": cluster["reused_from_version"],
                }
                for _, cluster in sorted(snapshot.clusters.items())
            ]
            result = {
                "ok": True,
                "topology_version": snapshot.topology_version,
                "topology_digest": snapshot.topology.digest,
                "result_version": snapshot.result_version,
                "event_watermark": snapshot.event_watermark,
                "window_ms": snapshot.window_ms,
                "active_window_count": len(active_windows),
                "cluster_count": len(snapshot.clusters),
                "invalidation_count": recompute_count,
                "total_invalidation_count": int(total_recompute),
                "invalidation_counts": invalidation_counts,
                "pending_jobs": [
                    {"job_id": row["job_id"], "kind": row["kind"]}
                    for row in pending
                ],
                "clusters": cluster_summaries,
                "root_candidate_summary": candidate_summaries,
            }
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def resume(self) -> dict[str, Any]:
        if not self.path.exists():
            fail("STATE_NOT_INITIALIZED", "State database does not exist")
        with self._file_lock():
            conn = self._connect()
            try:
                self._require_initialized(conn)
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = 'pending' ORDER BY created_order"
                ).fetchall()
            finally:
                conn.close()
            results = []
            errors = []
            for row in rows:
                job_id = row["job_id"]
                kind = row["kind"]
                payload = _json(row["payload_json"])
                try:
                    conn = self._connect()
                    try:
                        result = self._apply_job(conn, job_id, kind, payload)
                        result["resumed"] = True
                        results.append(result)
                    finally:
                        conn.close()
                except AlarmError as exc:
                    errors.append(exc.to_dict())
                    conn = self._connect()
                    try:
                        with self._write_txn(conn):
                            conn.execute(
                                "UPDATE jobs SET status = 'failed', result_json = ? WHERE job_id = ?",
                                (canonical_json(exc.to_dict()), job_id),
                            )
                    finally:
                        conn.close()
                except Exception as exc:
                    error = AlarmError(
                        "INTERNAL_ERROR",
                        "Persisted operation failed during replay",
                        exception_type=type(exc).__name__,
                        reason=str(exc),
                    ).to_dict()
                    errors.append(error)
                    conn = self._connect()
                    try:
                        with self._write_txn(conn):
                            conn.execute(
                                "UPDATE jobs SET status = 'failed', result_json = ? WHERE job_id = ?",
                                (canonical_json(error), job_id),
                            )
                    finally:
                        conn.close()
            return {
                "ok": len(errors) == 0,
                "operation": "resume",
                "replayed_count": len(results),
                "failed_count": len(errors),
                "results": results,
                "errors": errors,
            }

    def verify(self) -> dict[str, Any]:
        if not self.path.exists():
            fail("STATE_NOT_INITIALIZED", "State database does not exist")
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            snapshot = self._snapshot(conn)
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            stage_count = conn.execute(
                "SELECT (SELECT COUNT(*) FROM stage_clusters) + "
                "(SELECT COUNT(*) FROM stage_candidates)"
            ).fetchone()[0]
            events = snapshot.events
            expected_clusters = build_all_clusters(
                events,
                snapshot.window_ms,
                snapshot.suppressed,
                snapshot.topology,
            )
            cluster_mismatches = sorted(
                key
                for key in set(expected_clusters) | set(snapshot.clusters)
                if key not in expected_clusters
                or key not in snapshot.clusters
                or _cluster_core(expected_clusters[key]) != _cluster_core(snapshot.clusters[key])
            )
            expected_candidates = build_all_candidates(
                snapshot.topology,
                expected_clusters.values(),
                events,
                snapshot.suppressed,
            )
            cluster_digest_mismatches = sorted(
                key
                for key, cluster in expected_clusters.items()
                if key not in snapshot.clusters
                or digest(_cluster_core(cluster)) != snapshot.clusters[key]["digest"]
            )
            candidate_digest_mismatches = sorted(
                key
                for key, candidate in expected_candidates.items()
                if key not in snapshot.candidates
                or candidate.digest != snapshot.candidates[key]["digest"]
            )
            candidate_mismatches = sorted(
                key
                for key in set(expected_candidates) | set(snapshot.candidates)
                if key not in expected_candidates
                or key not in snapshot.candidates
                or expected_candidates[key].payload != snapshot.candidates[key]["payload"]
            )
            watermark = max((event["event_seq"] for event in events), default=0)
            suppression_seqs = {
                int(row[0])
                for row in conn.execute("SELECT event_seq FROM event_suppressions").fetchall()
            }
            suppression_missing = sorted(
                event["event_seq"]
                for event in events
                if event["event_seq"] not in suppression_seqs
            )
            checks = {
                "sqlite_integrity_ok": integrity == "ok",
                "stage_tables_empty": int(stage_count) == 0,
                "event_watermark_matches": watermark == snapshot.event_watermark,
                "suppression_ledger_complete": not suppression_missing,
                "cluster_pointer_matches_run": _meta(conn, "current_result_version")
                == str(snapshot.result_version),
                "cluster_count": len(snapshot.clusters),
                "candidate_count": len(snapshot.candidates),
                "recomputed_cluster_count": len(expected_clusters),
                "recomputed_candidate_count": len(expected_candidates),
                "cluster_mismatches": cluster_mismatches,
                "candidate_mismatches": candidate_mismatches,
                "cluster_digest_mismatches": cluster_digest_mismatches,
                "candidate_digest_mismatches": candidate_digest_mismatches,
                "missing_suppression_event_seqs": suppression_missing,
            }
            ok = (
                integrity == "ok"
                and int(stage_count) == 0
                and watermark == snapshot.event_watermark
                and not suppression_missing
                and not cluster_mismatches
                and not candidate_mismatches
                and not cluster_digest_mismatches
                and not candidate_digest_mismatches
            )
            conn.commit()
            return {
                "ok": ok,
                "operation": "verify",
                "topology_version": snapshot.topology_version,
                "result_version": snapshot.result_version,
                "checks": checks,
            }
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _maybe_crash(self, point: str) -> None:
        if os.environ.get("ALARM_DEDUP_TEST_CRASH_POINT") == point:
            os._exit(91)
