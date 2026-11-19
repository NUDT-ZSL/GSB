"""SQLite persistence, file locking, and transaction primitives."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .errors import ErrorCode, FlowGuardError
from .models import Event, Sample
from .util import TOKEN_SCALE, canonical_json, sha256_text


SCHEMA_VERSION = 1


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS services (
    name TEXT PRIMARY KEY,
    priority INTEGER NOT NULL,
    instances_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependencies (
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    PRIMARY KEY (source, target),
    FOREIGN KEY (source) REFERENCES services(name),
    FOREIGN KEY (target) REFERENCES services(name)
);

CREATE TABLE IF NOT EXISTS policies (
    service TEXT PRIMARY KEY,
    policy_json TEXT NOT NULL,
    FOREIGN KEY (service) REFERENCES services(name)
);

CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('success', 'error')),
    latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
    cost_weight REAL NOT NULL CHECK (cost_weight > 0),
    payload_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('loaded', 'staged', 'processed'))
);

CREATE TABLE IF NOT EXISTS service_state (
    service TEXT PRIMARY KEY,
    last_clock_ms INTEGER,
    last_event_seq INTEGER NOT NULL DEFAULT 0,
    token_units INTEGER NOT NULL,
    token_last_clock_ms INTEGER NOT NULL DEFAULT 0,
    breaker_state TEXT NOT NULL CHECK (breaker_state IN ('closed', 'open', 'half_open')),
    opened_until_ms INTEGER,
    half_open_probes_used INTEGER NOT NULL DEFAULT 0,
    half_open_successes INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    degrade_active INTEGER NOT NULL DEFAULT 0,
    degrade_level INTEGER NOT NULL,
    degrade_allow_ratio REAL NOT NULL,
    recovery_successes INTEGER NOT NULL DEFAULT 0,
    recovery_stable_windows INTEGER NOT NULL DEFAULT 0,
    recovery_epoch INTEGER NOT NULL DEFAULT 0,
    last_recovery_window_id INTEGER,
    stats_mode TEXT NOT NULL CHECK (stats_mode IN ('exact', 'layered')),
    current_window_id INTEGER,
    current_window_version TEXT,
    last_transition_reason TEXT NOT NULL DEFAULT '',
    updated_seq INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (service) REFERENCES services(name)
);

CREATE TABLE IF NOT EXISTS segment_snapshots (
    service TEXT NOT NULL,
    segment_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    event_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    success_count INTEGER NOT NULL,
    allowed_count INTEGER NOT NULL,
    cost_units INTEGER NOT NULL,
    max_latency_ms REAL NOT NULL,
    histogram_json TEXT NOT NULL,
    last_seq INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (service, segment_id, version),
    FOREIGN KEY (service) REFERENCES services(name)
);

CREATE TABLE IF NOT EXISTS segment_heads (
    service TEXT NOT NULL,
    segment_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY (service, segment_id),
    FOREIGN KEY (service, segment_id, version)
        REFERENCES segment_snapshots(service, segment_id, version)
);

CREATE TABLE IF NOT EXISTS window_snapshots (
    service TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    stats_mode TEXT NOT NULL CHECK (stats_mode IN ('exact', 'layered')),
    event_count INTEGER NOT NULL,
    error_count INTEGER NOT NULL,
    error_rate REAL NOT NULL,
    quantile REAL NOT NULL,
    quantile_lower REAL NOT NULL,
    quantile_upper REAL NOT NULL,
    error_upper_bound REAL NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    PRIMARY KEY (service, window_id, version),
    FOREIGN KEY (service) REFERENCES services(name)
);

CREATE TABLE IF NOT EXISTS window_heads (
    service TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    version TEXT NOT NULL,
    PRIMARY KEY (service, window_id),
    FOREIGN KEY (service, window_id, version)
        REFERENCES window_snapshots(service, window_id, version)
);

CREATE TABLE IF NOT EXISTS window_events (
    seq INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    segment_id INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    result TEXT NOT NULL,
    latency_ms REAL NOT NULL,
    cost_weight REAL NOT NULL,
    allowed INTEGER NOT NULL,
    window_id INTEGER NOT NULL,
    FOREIGN KEY (seq) REFERENCES events(seq),
    FOREIGN KEY (service) REFERENCES services(name)
);

CREATE TABLE IF NOT EXISTS event_windows (
    seq INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    segment_id INTEGER NOT NULL,
    window_id INTEGER NOT NULL,
    window_version TEXT NOT NULL,
    stats_mode TEXT NOT NULL,
    FOREIGN KEY (seq) REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS decisions (
    seq INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    final_decision TEXT NOT NULL,
    source TEXT NOT NULL,
    window_id INTEGER NOT NULL,
    window_version TEXT NOT NULL,
    stats_mode TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    FOREIGN KEY (seq) REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS pending_jobs (
    seq INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'committed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    FOREIGN KEY (seq) REFERENCES events(seq)
);

CREATE TABLE IF NOT EXISTS recovery_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    seq INTEGER,
    service TEXT,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_service_clock
    ON events(service, clock_ms);
CREATE INDEX IF NOT EXISTS idx_window_events_service_segment
    ON window_events(service, segment_id, seq);
CREATE INDEX IF NOT EXISTS idx_decisions_service
    ON decisions(service, seq);
CREATE INDEX IF NOT EXISTS idx_pending_jobs_status
    ON pending_jobs(status, seq);
"""


class Store:
    """Owns all durable governance state for one database file."""

    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.lock_path = Path(str(self.path) + ".lock")

    def exists(self) -> bool:
        return self.path.exists()

    def is_loaded(self) -> bool:
        if not self.path.exists():
            return False
        try:
            conn = sqlite3.connect(self.path)
            try:
                row = conn.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()
                return row is not None
            finally:
                conn.close()
        except sqlite3.Error:
            return False

    @contextlib.contextmanager
    def file_lock(self) -> Iterator[None]:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path,
            timeout=30.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA wal_autocheckpoint = 1000")
        try:
            conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.DatabaseError:
            pass
        return conn

    @contextlib.contextmanager
    def transaction(self, conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise

    def initialize(self, sample: Sample, reset: bool = False) -> dict[str, Any]:
        with self.file_lock():
            if reset:
                for suffix in ("", "-wal", "-shm"):
                    candidate = Path(str(self.path) + suffix)
                    with contextlib.suppress(FileNotFoundError):
                        candidate.unlink()

            conn = self.connect()
            try:
                conn.executescript(SCHEMA)
                with self.transaction(conn):
                    existing = _meta_get(conn, "topology_digest")
                    if existing is not None:
                        same = (
                            existing == sample.topology_digest
                            and _meta_get(conn, "events_digest")
                            == sample.events_digest
                            and _meta_get(conn, "policy_digest")
                            == sample.policy_digest
                        )
                        if same:
                            return {
                                "ok": True,
                                "idempotent": True,
                                "loaded_events": 0,
                                "digests": sample.digests(),
                            }
                        raise FlowGuardError(
                            ErrorCode.SAMPLE_DIGEST_MISMATCH,
                            "database already contains a different sample",
                            {
                                "expected": {
                                    "topology": existing,
                                    "events": _meta_get(conn, "events_digest"),
                                    "policy": _meta_get(conn, "policy_digest"),
                                },
                                "received": sample.digests(),
                            },
                        )

                    topology_normalized = sample.topology.normalized()
                    policy_normalized = sample.policies.normalized()
                    _meta_set(conn, "schema_version", str(SCHEMA_VERSION))
                    _meta_set(conn, "topology_digest", sample.topology_digest)
                    _meta_set(conn, "events_digest", sample.events_digest)
                    _meta_set(conn, "policy_digest", sample.policy_digest)
                    _meta_set(conn, "topology_json", canonical_json(topology_normalized))
                    _meta_set(conn, "policy_json", canonical_json(policy_normalized))
                    _meta_set(conn, "last_processed_seq", "0")

                    for service in sample.topology.services:
                        conn.execute(
                            "INSERT INTO services(name, priority, instances_json) VALUES (?, ?, ?)",
                            (
                                service.name,
                                service.priority,
                                canonical_json(list(service.instances)),
                            ),
                        )
                    for source, target in sample.topology.dependencies:
                        conn.execute(
                            "INSERT INTO dependencies(source, target) VALUES (?, ?)",
                            (source, target),
                        )
                    for service, policy in sorted(sample.policies.by_service.items()):
                        conn.execute(
                            "INSERT INTO policies(service, policy_json) VALUES (?, ?)",
                            (service, canonical_json(policy.normalized())),
                        )
                        conn.execute(
                            """
                            INSERT INTO service_state(
                                service, token_units, breaker_state, degrade_level,
                                degrade_allow_ratio, stats_mode
                            ) VALUES (?, ?, 'closed', ?, ?, 'exact')
                            """,
                            (
                                service,
                                policy.token_bucket_capacity * TOKEN_SCALE,
                                policy.degrade_level,
                                policy.degrade_allow_ratio,
                            ),
                        )
                    for event in sample.events:
                        conn.execute(
                            """
                            INSERT INTO events(
                                seq, service, clock_ms, result, latency_ms,
                                cost_weight, payload_digest, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'loaded')
                            """,
                            (
                                event.seq,
                                event.service,
                                event.clock_ms,
                                event.result,
                                event.latency_ms,
                                event.cost_weight,
                                event.digest(),
                            ),
                        )
                return {
                    "ok": True,
                    "idempotent": False,
                    "loaded_events": len(sample.events),
                    "loaded_services": len(sample.topology.services),
                    "digests": sample.digests(),
                }
            finally:
                conn.close()

    def ingest_new_events(self, events: tuple[Event, ...]) -> dict[str, Any]:
        """Append validated events to an existing loaded sample."""

        with self.file_lock():
            conn = self.connect()
            try:
                with self.transaction(conn):
                    if _meta_get(conn, "topology_digest") is None:
                        raise FlowGuardError(
                            ErrorCode.NOT_FOUND,
                            "database has not been loaded",
                        )
                    max_seq = int(
                        conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
                    )
                    inserted = 0
                    for event in events:
                        if event.seq <= max_seq:
                            existing = conn.execute(
                                "SELECT payload_digest FROM events WHERE seq = ?",
                                (event.seq,),
                            ).fetchone()
                            if existing is None:
                                raise FlowGuardError(
                                    ErrorCode.EVENT_SEQ_NOT_INCREASING,
                                    "new event sequence must be greater than existing events",
                                    {"seq": event.seq, "existing_max_seq": max_seq},
                                )
                            if existing["payload_digest"] != event.digest():
                                raise FlowGuardError(
                                    ErrorCode.EVENT_SEQ_CONFLICT,
                                    "event sequence exists with different content",
                                    {"seq": event.seq},
                                )
                            continue
                        conn.execute(
                            """
                            INSERT INTO events(
                                seq, service, clock_ms, result, latency_ms,
                                cost_weight, payload_digest, status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'loaded')
                            """,
                            (
                                event.seq,
                                event.service,
                                event.clock_ms,
                                event.result,
                                event.latency_ms,
                                event.cost_weight,
                                event.digest(),
                            ),
                        )
                        max_seq = event.seq
                        inserted += 1
                return {"ok": True, "inserted_events": inserted}
            finally:
                conn.close()

    def stage_event(
        self,
        conn: sqlite3.Connection,
        event: Event,
        policy_tolerance_ms: int,
    ) -> tuple[str, str | None]:
        """Stage one event and its recovery job in the caller's transaction."""

        existing = conn.execute(
            "SELECT * FROM events WHERE seq = ?",
            (event.seq,),
        ).fetchone()
        if existing is not None:
            if existing["payload_digest"] != event.digest():
                raise FlowGuardError(
                    ErrorCode.EVENT_SEQ_CONFLICT,
                    "event sequence exists with different content",
                    {"seq": event.seq},
                )
            decision = conn.execute(
                "SELECT payload_json FROM decisions WHERE seq = ?",
                (event.seq,),
            ).fetchone()
            if decision is not None:
                return "processed", decision["payload_json"]
            last_clock_raw = _meta_get(conn, "last_clock_ms")
            if last_clock_raw is not None:
                last_clock = int(last_clock_raw)
                if event.clock_ms < last_clock - policy_tolerance_ms:
                    raise FlowGuardError(
                        ErrorCode.CLOCK_ROLLBACK,
                        "logical clock rollback exceeds tolerance",
                        {
                            "seq": event.seq,
                            "clock_ms": event.clock_ms,
                            "last_clock_ms": last_clock,
                            "tolerance_ms": policy_tolerance_ms,
                        },
                    )
            row = conn.execute(
                "SELECT status FROM pending_jobs WHERE seq = ?",
                (event.seq,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO pending_jobs(seq, service, status) VALUES (?, ?, 'pending')",
                    (event.seq, event.service),
                )
                conn.execute(
                    "UPDATE events SET status = 'staged' WHERE seq = ?",
                    (event.seq,),
                )
            return "pending", None

        max_seq = int(
            conn.execute("SELECT COALESCE(MAX(seq), 0) FROM events").fetchone()[0]
        )
        if event.seq <= max_seq:
            raise FlowGuardError(
                ErrorCode.EVENT_SEQ_NOT_INCREASING,
                "event sequence is behind an existing event",
                {"seq": event.seq, "existing_max_seq": max_seq},
            )

        last_clock_raw = _meta_get(conn, "last_clock_ms")
        if last_clock_raw is not None:
            last_clock = int(last_clock_raw)
            if event.clock_ms < last_clock - policy_tolerance_ms:
                raise FlowGuardError(
                    ErrorCode.CLOCK_ROLLBACK,
                    "logical clock rollback exceeds tolerance",
                    {
                        "seq": event.seq,
                        "clock_ms": event.clock_ms,
                        "last_clock_ms": last_clock,
                        "tolerance_ms": policy_tolerance_ms,
                    },
                )
        conn.execute(
            """
            INSERT INTO events(
                seq, service, clock_ms, result, latency_ms, cost_weight,
                payload_digest, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'staged')
            """,
            (
                event.seq,
                event.service,
                event.clock_ms,
                event.result,
                event.latency_ms,
                event.cost_weight,
                event.digest(),
            ),
        )
        conn.execute(
            "INSERT INTO pending_jobs(seq, service, status) VALUES (?, ?, 'pending')",
            (event.seq, event.service),
        )
        return "pending", None

    def pending_events(self) -> list[sqlite3.Row]:
        conn = self.connect()
        try:
            return list(
                conn.execute(
                    """
                    SELECT e.* FROM events e
                    LEFT JOIN decisions d ON d.seq = e.seq
                    WHERE d.seq IS NULL
                    ORDER BY e.seq
                    """
                )
            )
        finally:
            conn.close()

    def next_pending_event(self) -> sqlite3.Row | None:
        conn = self.connect()
        try:
            return conn.execute(
                """
                SELECT e.* FROM events e
                LEFT JOIN decisions d ON d.seq = e.seq
                WHERE d.seq IS NULL
                ORDER BY e.seq
                LIMIT 1
                """
            ).fetchone()
        finally:
            conn.close()

    def get_decision(self, seq: int) -> dict[str, Any] | None:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT payload_json FROM decisions WHERE seq = ?",
                (seq,),
            ).fetchone()
            if row is None:
                return None
            return json.loads(row["payload_json"])
        finally:
            conn.close()

    def get_policy_row(self, conn: sqlite3.Connection, service: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT policy_json FROM policies WHERE service = ?",
            (service,),
        ).fetchone()
        if row is None:
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "service policy not found",
                {"service": service},
            )
        return json.loads(row["policy_json"])

    def get_state(self, conn: sqlite3.Connection, service: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM service_state WHERE service = ?",
            (service,),
        ).fetchone()
        if row is None:
            raise FlowGuardError(
                ErrorCode.NOT_FOUND,
                "service state not found",
                {"service": service},
            )
        return row

    def decision_digest(self) -> str:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT seq, digest FROM decisions ORDER BY seq"
            ).fetchall()
            return sha256_text(canonical_json([dict(row) for row in rows]))
        finally:
            conn.close()

    def counts(self) -> dict[str, Any]:
        conn = self.connect()
        try:
            event_row = conn.execute(
                """
                SELECT
                  COUNT(*) AS total,
                  SUM(CASE WHEN status = 'processed' THEN 1 ELSE 0 END) AS processed,
                  SUM(CASE WHEN status != 'processed' THEN 1 ELSE 0 END) AS pending
                FROM events
                """
            ).fetchone()
            decision_rows = conn.execute(
                """
                SELECT final_decision, COUNT(*) AS count
                FROM decisions GROUP BY final_decision ORDER BY final_decision
                """
            ).fetchall()
            mode_rows = conn.execute(
                """
                SELECT stats_mode, COUNT(*) AS count
                FROM decisions GROUP BY stats_mode ORDER BY stats_mode
                """
            ).fetchall()
            return {
                "events": {
                    "total": int(event_row["total"] or 0),
                    "processed": int(event_row["processed"] or 0),
                    "pending": int(event_row["pending"] or 0),
                },
                "decisions": {
                    row["final_decision"]: int(row["count"])
                    for row in decision_rows
                },
                "modes": {
                    row["stats_mode"]: int(row["count"])
                    for row in mode_rows
                },
            }
        finally:
            conn.close()


def _meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def meta_get(conn: sqlite3.Connection, key: str) -> str | None:
    return _meta_get(conn, key)


def meta_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    _meta_set(conn, key, value)


def sqlite_busy(exc: sqlite3.OperationalError, fields: dict[str, Any] | None = None) -> FlowGuardError:
    message = str(exc)
    if "locked" in message.lower() or "busy" in message.lower():
        return FlowGuardError(
            ErrorCode.DB_BUSY,
            "database is busy",
            fields or {},
            exit_code=4,
        )
    return FlowGuardError(
        ErrorCode.STATE_INCONSISTENT,
        "SQLite operation failed",
        {"reason": message, **(fields or {})},
    )


def remove_database_files(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            os.remove(candidate)
