"""SQLite persistence, transaction helpers, locks, and operation journal."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json, sha256_value
from .errors import EngineError, error
from .models import Candidate
from .validation import BaseCandidate


SCHEMA_VERSION = 1


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clock_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    watermark_ms INTEGER,
    tolerance_ms INTEGER NOT NULL DEFAULT 250,
    updated_by_op TEXT
);

CREATE TABLE IF NOT EXISTS package_loads (
    load_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    package_digest TEXT NOT NULL,
    package_path TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_candidates (
    source_fingerprint TEXT PRIMARY KEY,
    package_digest TEXT NOT NULL,
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    source_format TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    effective_at_ms INTEGER,
    relative_effective_after_s INTEGER,
    expires_at_ms INTEGER,
    params_json TEXT NOT NULL,
    upstreams_json TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    original_time_json TEXT NOT NULL,
    source_ref_json TEXT NOT NULL,
    created_by_op TEXT,
    CHECK (tombstone IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_source_candidates_target
    ON source_candidates(service, environment, generation);

CREATE TABLE IF NOT EXISTS migration_decisions (
    migration_id TEXT PRIMARY KEY,
    source_fingerprint TEXT NOT NULL UNIQUE,
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    source_format TEXT NOT NULL,
    source_ref_json TEXT NOT NULL,
    source_relative_seconds INTEGER NOT NULL,
    migration_clock_ms INTEGER NOT NULL,
    scale_ms_per_second INTEGER NOT NULL,
    rounding_direction TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    original_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    created_by_op TEXT
);

CREATE TABLE IF NOT EXISTS config_versions (
    candidate_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    generation INTEGER NOT NULL,
    source_format TEXT NOT NULL,
    source_priority INTEGER NOT NULL,
    version TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER,
    params_json TEXT NOT NULL,
    upstreams_json TEXT NOT NULL,
    tombstone INTEGER NOT NULL,
    original_time_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    source_ref_json TEXT NOT NULL,
    origin TEXT NOT NULL,
    event_id TEXT,
    event_source TEXT,
    accepted INTEGER NOT NULL DEFAULT 1,
    created_by_op TEXT,
    CHECK (tombstone IN (0, 1)),
    CHECK (accepted IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_config_versions_target
    ON config_versions(service, environment, generation, effective_at_ms);

CREATE INDEX IF NOT EXISTS idx_config_versions_event
    ON config_versions(event_source, event_id);

CREATE TABLE IF NOT EXISTS operations (
    op_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    request_json TEXT NOT NULL,
    state TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    tolerance_ms INTEGER NOT NULL,
    result_json TEXT,
    error_json TEXT,
    created_seq INTEGER NOT NULL,
    updated_seq INTEGER NOT NULL,
    CHECK (state IN ('pending', 'running', 'committed', 'failed'))
);

CREATE INDEX IF NOT EXISTS idx_operations_state ON operations(state, created_seq);

CREATE TABLE IF NOT EXISTS operation_steps (
    op_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    step_name TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT,
    result_json TEXT,
    updated_seq INTEGER NOT NULL,
    PRIMARY KEY (op_id, position),
    UNIQUE (op_id, step_name),
    FOREIGN KEY (op_id) REFERENCES operations(op_id) ON DELETE CASCADE,
    CHECK (state IN ('pending', 'running', 'committed'))
);

CREATE TABLE IF NOT EXISTS event_streams (
    source TEXT PRIMARY KEY,
    committed_seq INTEGER NOT NULL DEFAULT 0,
    watermark_clock_ms INTEGER NOT NULL DEFAULT 0,
    last_event_id TEXT,
    updated_by_op TEXT
);

CREATE TABLE IF NOT EXISTS event_commits (
    source TEXT NOT NULL,
    seq INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    content_digest TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    event_json TEXT NOT NULL,
    candidate_id TEXT,
    committed_by_op TEXT,
    committed_seq INTEGER NOT NULL,
    PRIMARY KEY (source, seq),
    UNIQUE (source, event_id),
    CHECK (status IN ('accepted', 'rejected', 'superseded'))
);

CREATE TABLE IF NOT EXISTS views (
    view_seq INTEGER PRIMARY KEY AUTOINCREMENT,
    view_version TEXT NOT NULL UNIQUE,
    clock_ms INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    created_by_op TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS published_recovery_snapshots (
    publication_id TEXT PRIMARY KEY,
    view_version TEXT NOT NULL,
    view_seq INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    event_watermarks_json TEXT NOT NULL,
    migration_decisions_json TEXT NOT NULL,
    state_transitions_json TEXT NOT NULL,
    op_id TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_published_recovery_latest
    ON published_recovery_snapshots(created_seq);

CREATE TABLE IF NOT EXISTS state_transitions (
    transition_id TEXT PRIMARY KEY,
    service TEXT NOT NULL,
    environment TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    previous_active_candidate_id TEXT,
    active_candidate_id TEXT,
    clock_ms INTEGER NOT NULL,
    view_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_by_op TEXT,
    created_seq INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_transitions_target
    ON state_transitions(service, environment, created_seq);

CREATE TABLE IF NOT EXISTS recovery_decisions (
    recovery_id TEXT PRIMARY KEY,
    op_id TEXT NOT NULL,
    action TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_seq INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS snapshot_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    event_commits INTEGER NOT NULL
);
"""


def _now_ms() -> int:
    # This is only an audit ordering value. Business time is always injected.
    return time.time_ns() // 1_000_000


def _json(value: Any) -> str:
    return canonical_json(value)


def _loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return __import__("json").loads(value)


def _seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(created_seq), 0) + 1 FROM operations").fetchone()
    return int(row[0])


def _transition_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(created_seq), 0) + 1 FROM state_transitions"
    ).fetchone()
    return int(row[0])


def _view_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(created_seq), 0) + 1 FROM views").fetchone()
    return int(row[0])


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(
            str(path),
            timeout=30,
            isolation_level=None,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn
    except sqlite3.Error as exc:
        raise error("INVALID_DB", f"cannot open SQLite database: {exc}", path=str(path)) from exc


@contextlib.contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    try:
        conn.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        raise error("INVALID_DB", f"cannot begin write transaction: {exc}") from exc
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextlib.contextmanager
def file_lock(db_path: str | Path, name: str) -> Iterator[None]:
    """Cross-process advisory lock; the OS releases it even after SIGKILL."""

    lock_dir = Path(f"{Path(db_path).resolve()}.locks")
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
    if len(safe) > 120:
        safe = hashlib.sha256(name.encode("utf-8")).hexdigest()
    path = lock_dir / f"{safe}.lock"
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def initialize(path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, "schema-init"):
        conn = connect(path)
        try:
            # DDL is idempotent and runs under the schema-init file lock.  The
            # metadata write below is transactional.
            try:
                conn.executescript(SCHEMA_SQL)
            except sqlite3.Error as exc:
                raise error(
                    "INVALID_DB", f"cannot create SQLite schema: {exc}", path=str(path)
                ) from exc
            with write_transaction(conn):
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?)",
                        (str(SCHEMA_VERSION),),
                    )
                elif int(row[0]) != SCHEMA_VERSION:
                    raise error(
                        "INVALID_DB",
                        "state database schema version is unsupported",
                        expected=SCHEMA_VERSION,
                        actual=int(row[0]),
                    )
                clock = conn.execute("SELECT singleton FROM clock_state").fetchone()
                if clock is None:
                    conn.execute(
                        "INSERT INTO clock_state(singleton, watermark_ms, tolerance_ms) "
                        "VALUES(1, NULL, 250)"
                    )
                snapshot = conn.execute("SELECT singleton FROM snapshot_state").fetchone()
                if snapshot is None:
                    conn.execute(
                        "INSERT INTO snapshot_state(singleton, event_commits) VALUES(1, 0)"
                    )
        except EngineError:
            raise
        except sqlite3.Error as exc:
            raise error("INVALID_DB", f"cannot initialize SQLite state: {exc}", path=str(path)) from exc
        finally:
            conn.close()


def record_load(
    conn: sqlite3.Connection,
    *,
    package_digest: str,
    package_path: str,
    summary: dict[str, Any],
) -> None:
    with write_transaction(conn):
        existing = conn.execute(
            "SELECT load_seq FROM package_loads WHERE package_digest=? AND package_path=? "
            "ORDER BY load_seq LIMIT 1",
            (package_digest, package_path),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO package_loads(package_digest, package_path, summary_json, created_ms) "
            "VALUES(?, ?, ?, ?)",
            (package_digest, package_path, _json(summary), _now_ms()),
        )


def get_clock(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        "SELECT watermark_ms, tolerance_ms, updated_by_op FROM clock_state WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise error("INVALID_DB", "clock state is missing")
    return {
        "tolerance_ms": int(row["tolerance_ms"]),
        "updated_by_op": row["updated_by_op"],
        "watermark_ms": row["watermark_ms"],
    }


def check_clock(
    conn: sqlite3.Connection,
    requested_ms: int,
    tolerance_ms: int,
) -> int:
    state = get_clock(conn)
    watermark = state["watermark_ms"]
    if watermark is not None and requested_ms < int(watermark) - tolerance_ms:
        raise error(
            "CLOCK_ROLLBACK",
            "logical clock rollback exceeds configured tolerance",
            requested_clock_ms=requested_ms,
            watermark_ms=int(watermark),
            tolerance_ms=tolerance_ms,
            rollback_ms=int(watermark) - requested_ms,
        )
    if watermark is None:
        return requested_ms
    return max(requested_ms, int(watermark))


def advance_clock(
    conn: sqlite3.Connection,
    effective_clock_ms: int,
    tolerance_ms: int,
    op_id: str,
) -> None:
    state = get_clock(conn)
    watermark = state["watermark_ms"]
    new_watermark = (
        effective_clock_ms if watermark is None else max(int(watermark), effective_clock_ms)
    )
    conn.execute(
        "UPDATE clock_state SET watermark_ms=?, tolerance_ms=?, updated_by_op=? "
        "WHERE singleton=1",
        (new_watermark, tolerance_ms, op_id),
    )


def begin_operation(
    conn: sqlite3.Connection,
    *,
    op_id: str,
    kind: str,
    request_digest: str,
    request: dict[str, Any],
    clock_ms: int,
    tolerance_ms: int,
    steps: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create or find a deterministic operation and its ordered step plan."""

    with write_transaction(conn):
        row = conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
        if row is not None:
            if row["kind"] != kind or row["request_digest"] != request_digest:
                raise error(
                    "OPERATION_CONFLICT",
                    "operation ID reused with different input",
                    op_id=op_id,
                )
            existing_steps = [
                dict(item)
                for item in conn.execute(
                    "SELECT * FROM operation_steps WHERE op_id=? ORDER BY position", (op_id,)
                ).fetchall()
            ]
            if [(item["position"], item["step_name"]) for item in existing_steps] != [
                (index, item["step_name"]) for index, item in enumerate(steps)
            ]:
                raise error(
                    "OPERATION_CONFLICT",
                    "operation step plan changed for identical input",
                    op_id=op_id,
                )
            return dict(row)
        seq = _seq(conn)
        conn.execute(
            "INSERT INTO operations(op_id, kind, request_digest, request_json, state, clock_ms, "
            "tolerance_ms, created_seq, updated_seq) VALUES(?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (op_id, kind, request_digest, _json(request), clock_ms, tolerance_ms, seq, seq),
        )
        for index, item in enumerate(steps):
            conn.execute(
                "INSERT INTO operation_steps(op_id, position, step_name, state, payload_json, "
                "updated_seq) VALUES(?, ?, ?, 'pending', ?, ?)",
                (op_id, index, item["step_name"], _json(item.get("payload", {})), seq),
            )
        row = conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
        return dict(row)


def get_operation(conn: sqlite3.Connection, op_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM operations WHERE op_id=?", (op_id,)).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["request"] = _loads(result.pop("request_json"), {})
    result["result"] = _loads(result.pop("result_json"))
    result["error"] = _loads(result.pop("error_json"))
    return result


def get_operation_steps(conn: sqlite3.Connection, op_id: str) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute(
        "SELECT * FROM operation_steps WHERE op_id=? ORDER BY position", (op_id,)
    ).fetchall():
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json"), {})
        item["result"] = _loads(item.pop("result_json"))
        result.append(item)
    return result


def mark_operation_running(conn: sqlite3.Connection, op_id: str) -> None:
    with write_transaction(conn):
        row = conn.execute("SELECT state FROM operations WHERE op_id=?", (op_id,)).fetchone()
        if row is None:
            raise error("INVALID_ARGUMENT", f"unknown operation: {op_id}")
        if row["state"] == "committed":
            return
        seq = _seq(conn)
        conn.execute(
            "UPDATE operations SET state='running', updated_seq=? WHERE op_id=?",
            (seq, op_id),
        )


def mark_step_running(conn: sqlite3.Connection, op_id: str, position: int) -> None:
    with write_transaction(conn):
        conn.execute(
            "UPDATE operation_steps SET state='running', updated_seq=? "
            "WHERE op_id=? AND position=? AND state!='committed'",
            (_transition_seq(conn), op_id, position),
        )


def complete_step(
    conn: sqlite3.Connection,
    op_id: str,
    position: int,
    result: dict[str, Any],
) -> None:
    conn.execute(
        "UPDATE operation_steps SET state='committed', result_json=?, updated_seq=? "
        "WHERE op_id=? AND position=?",
        (_json(result), _transition_seq(conn), op_id, position),
    )


def fail_operation(
    conn: sqlite3.Connection,
    op_id: str,
    failure: dict[str, Any],
) -> None:
    with write_transaction(conn):
        seq = _seq(conn)
        conn.execute(
            "UPDATE operations SET state='failed', error_json=?, updated_seq=? WHERE op_id=?",
            (_json(failure), seq, op_id),
        )


def finish_operation(
    conn: sqlite3.Connection,
    op_id: str,
    result: dict[str, Any],
) -> None:
    seq = _seq(conn)
    conn.execute(
        "UPDATE operations SET state='committed', result_json=?, updated_seq=? WHERE op_id=?",
        (_json(result), seq, op_id),
    )


def list_recoverable_operations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM operations WHERE state IN ('pending', 'running') ORDER BY created_seq"
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["request"] = _loads(item.pop("request_json"), {})
        item["result"] = _loads(item.pop("result_json"))
        item["error"] = _loads(item.pop("error_json"))
        result.append(item)
    return result


def add_recovery_decision(
    conn: sqlite3.Connection,
    *,
    op_id: str,
    action: str,
    details: dict[str, Any],
) -> None:
    recovery_id = sha256_value({"action": action, "details": details, "op_id": op_id})
    seq = _transition_seq(conn)
    conn.execute(
        "INSERT OR IGNORE INTO recovery_decisions(recovery_id, op_id, action, details_json, "
        "created_seq) VALUES(?, ?, ?, ?, ?)",
        (recovery_id, op_id, action, _json(details), seq),
    )


def source_fingerprint(candidate: BaseCandidate) -> str:
    return sha256_value(
        {
            "content_digest": candidate.content_digest,
            "environment": candidate.environment,
            "generation": candidate.generation,
            "service": candidate.service,
            "source_format": candidate.source_format,
            "source_ref": candidate.source_ref,
            "version": candidate.version,
        }
    )


def upsert_source_candidate(
    conn: sqlite3.Connection,
    candidate: BaseCandidate,
    *,
    package_digest: str,
    op_id: str,
) -> str:
    fingerprint = source_fingerprint(candidate)
    row = conn.execute(
        "SELECT * FROM source_candidates WHERE source_fingerprint=?", (fingerprint,)
    ).fetchone()
    if row is not None:
        if row["content_digest"] != candidate.content_digest:
            raise error(
                "GENERATION_CONFLICT",
                "source fingerprint collided with different content",
                service=candidate.service,
                environment=candidate.environment,
            )
        return fingerprint
    conn.execute(
        "INSERT INTO source_candidates(source_fingerprint, package_digest, service, environment, "
        "logical_clock_ms, generation, source_format, source_priority, version, content_digest, "
        "effective_at_ms, relative_effective_after_s, expires_at_ms, params_json, upstreams_json, "
        "tombstone, original_time_json, source_ref_json, created_by_op) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            fingerprint,
            package_digest,
            candidate.service,
            candidate.environment,
            candidate.logical_clock_ms,
            candidate.generation,
            candidate.source_format,
            candidate.source_priority,
            candidate.version,
            candidate.content_digest,
            candidate.absolute_effective_at_ms,
            candidate.relative_effective_after_s,
            candidate.expires_at_ms,
            _json(candidate.params),
            _json(candidate.upstreams),
            int(candidate.tombstone),
            _json(candidate.original_time()),
            _json(candidate.source_ref),
            op_id,
        ),
    )
    return fingerprint


def get_migration_by_fingerprint(
    conn: sqlite3.Connection, fingerprint: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM migration_decisions WHERE source_fingerprint=?", (fingerprint,)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["source_ref"] = _loads(result.pop("source_ref_json"), {})
    result["original"] = _loads(result.pop("original_json"), {})
    result["decision"] = _loads(result.pop("decision_json"), {})
    return result


def insert_migration(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
    candidate: BaseCandidate,
    migration_clock_ms: int,
    effective_at_ms: int,
    op_id: str,
) -> dict[str, Any]:
    existing = get_migration_by_fingerprint(conn, fingerprint)
    if existing is not None:
        if int(existing["effective_at_ms"]) != effective_at_ms:
            raise error(
                "GENERATION_CONFLICT",
                "migration decision changed for identical candidate",
                service=candidate.service,
                environment=candidate.environment,
            )
        return existing
    decision = {
        "after_ms": effective_at_ms,
        "before": candidate.relative_effective_after_s,
        "decision": "relative_to_injected_clock",
        "rounding_direction": "toward_positive_infinity",
        "scale_ms_per_second": 1000,
    }
    migration_id = sha256_value(
        {
            "fingerprint": fingerprint,
            "migration_clock_ms": migration_clock_ms,
            "source_relative_seconds": candidate.relative_effective_after_s,
        }
    )
    conn.execute(
        "INSERT INTO migration_decisions(migration_id, source_fingerprint, service, environment, "
        "source_format, source_ref_json, source_relative_seconds, migration_clock_ms, "
        "scale_ms_per_second, rounding_direction, effective_at_ms, original_json, decision_json, "
        "created_by_op) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1000, ?, ?, ?, ?, ?)",
        (
            migration_id,
            fingerprint,
            candidate.service,
            candidate.environment,
            candidate.source_format,
            _json(candidate.source_ref),
            candidate.relative_effective_after_s,
            migration_clock_ms,
            "toward_positive_infinity",
            effective_at_ms,
            _json(candidate.original_time()),
            _json(decision),
            op_id,
        ),
    )
    return get_migration_by_fingerprint(conn, fingerprint) or {}


def max_generation(conn: sqlite3.Connection, service: str, environment: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(generation), 0) FROM config_versions "
        "WHERE service=? AND environment=?",
        (service, environment),
    ).fetchone()
    return int(row[0])


def insert_config_version(
    conn: sqlite3.Connection,
    candidate: Candidate,
    *,
    op_id: str,
    accepted: bool = True,
) -> None:
    row = conn.execute(
        "SELECT * FROM config_versions WHERE candidate_id=?", (candidate.candidate_id,)
    ).fetchone()
    if row is not None:
        if row["content_digest"] != candidate.content_digest:
            raise error(
                "GENERATION_CONFLICT",
                "candidate ID reused with different content",
                candidate_id=candidate.candidate_id,
            )
        return
    head = max_generation(conn, candidate.service, candidate.environment)
    if candidate.origin == "base" and candidate.generation < head:
        # Base history is legitimate and immutable, but a later published head
        # must never be rolled back by a stale merge.
        raise error(
            "GENERATION_REGRESSION",
            "base candidate would roll back the committed generation",
            service=candidate.service,
            environment=candidate.environment,
            committed_generation=head,
            candidate_generation=candidate.generation,
        )
    conn.execute(
        "INSERT INTO config_versions(candidate_id, service, environment, logical_clock_ms, "
        "generation, source_format, source_priority, version, content_digest, effective_at_ms, "
        "expires_at_ms, params_json, upstreams_json, tombstone, original_time_json, decision_json, "
        "source_ref_json, origin, event_id, event_source, accepted, created_by_op) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            candidate.candidate_id,
            candidate.service,
            candidate.environment,
            candidate.logical_clock_ms,
            candidate.generation,
            candidate.source_format,
            candidate.source_priority,
            candidate.version,
            candidate.content_digest,
            candidate.effective_at_ms,
            candidate.expires_at_ms,
            _json(candidate.params),
            _json(candidate.upstreams),
            int(candidate.tombstone),
            _json(candidate.original_time),
            _json(candidate.decision),
            _json(candidate.source_ref),
            candidate.origin,
            candidate.event_id,
            candidate.event_source,
            int(accepted),
            op_id,
        ),
    )


def set_candidate_acceptance(
    conn: sqlite3.Connection,
    candidate_id: str,
    accepted: bool,
) -> None:
    conn.execute(
        "UPDATE config_versions SET accepted=? WHERE candidate_id=?",
        (int(accepted), candidate_id),
    )


def candidate_from_row(row: sqlite3.Row | dict[str, Any]) -> Candidate:
    value = dict(row)
    return Candidate(
        candidate_id=value["candidate_id"],
        service=value["service"],
        environment=value["environment"],
        logical_clock_ms=int(value["logical_clock_ms"]),
        generation=int(value["generation"]),
        source_format=value["source_format"],
        source_priority=int(value["source_priority"]),
        version=value["version"],
        content_digest=value["content_digest"],
        effective_at_ms=int(value["effective_at_ms"]),
        expires_at_ms=int(value["expires_at_ms"]) if value.get("expires_at_ms") is not None else None,
        params=_loads(value["params_json"], {}),
        upstreams=_loads(value["upstreams_json"], []),
        tombstone=bool(value["tombstone"]),
        original_time=_loads(value["original_time_json"], {}),
        decision=_loads(value["decision_json"], {}),
        source_ref=_loads(value["source_ref_json"], {}),
        origin=value.get("origin", "base"),
        event_id=value.get("event_id"),
        event_source=value.get("event_source"),
        accepted=bool(value.get("accepted", True)),
    )


def list_candidates(
    conn: sqlite3.Connection,
    *,
    target: tuple[str, str] | None = None,
    accepted_only: bool = True,
    origin: str | None = None,
) -> list[Candidate]:
    clauses: list[str] = []
    params: list[Any] = []
    if target is not None:
        clauses.append("service=? AND environment=?")
        params.extend(target)
    if accepted_only:
        clauses.append("accepted=1")
    if origin is not None:
        clauses.append("origin=?")
        params.append(origin)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM config_versions {where} "
        "ORDER BY service, environment, generation, effective_at_ms, candidate_id",
        params,
    ).fetchall()
    return [candidate_from_row(row) for row in rows]


def get_candidate(conn: sqlite3.Connection, candidate_id: str) -> Candidate | None:
    row = conn.execute(
        "SELECT * FROM config_versions WHERE candidate_id=?", (candidate_id,)
    ).fetchone()
    return candidate_from_row(row) if row is not None else None


def list_targets(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT DISTINCT service, environment FROM config_versions "
        "ORDER BY service, environment"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def get_stream(conn: sqlite3.Connection, source: str) -> dict[str, Any]:
    conn.execute(
        "INSERT OR IGNORE INTO event_streams(source, committed_seq, watermark_clock_ms) "
        "VALUES(?, 0, 0)",
        (source,),
    )
    row = conn.execute("SELECT * FROM event_streams WHERE source=?", (source,)).fetchone()
    return dict(row)


def get_event_commit(conn: sqlite3.Connection, source: str, seq: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM event_commits WHERE source=? AND seq=?", (source, seq)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["event"] = _loads(result.pop("event_json"), {})
    return result


def get_event_commit_by_id(
    conn: sqlite3.Connection, source: str, event_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM event_commits WHERE source=? AND event_id=?", (source, event_id)
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["event"] = _loads(result.pop("event_json"), {})
    return result


def insert_event_commit(
    conn: sqlite3.Connection,
    *,
    source: str,
    seq: int,
    event: dict[str, Any],
    status: str,
    reason: str | None,
    candidate_id: str | None,
    op_id: str,
) -> None:
    stream = get_stream(conn, source)
    if seq <= int(stream["committed_seq"]):
        existing = get_event_commit(conn, source, seq)
        if existing is None:
            raise error(
                "EVENT_SEQ_CONFLICT",
                "event sequence is covered by watermark but has no commit",
                source=source,
                seq=seq,
            )
        same = (
            existing["event_id"] == event["event_id"]
            and existing["content_digest"] == event["content_digest"]
        )
        if not same:
            raise error(
                "EVENT_SEQ_CONFLICT",
                "one event sequence number has conflicting content",
                source=source,
                seq=seq,
                committed_event_id=existing["event_id"],
                event_id=event["event_id"],
            )
        return
    expected = int(stream["committed_seq"]) + 1
    if seq != expected:
        raise error(
            "EVENT_SEQUENCE_GAP",
            "event stream cannot skip a sequence number",
            source=source,
            expected_seq=expected,
            actual_seq=seq,
        )
    if get_event_commit_by_id(conn, source, event["event_id"]) is not None:
        raise error(
            "EVENT_SEQUENCE_INVALID",
            "event ID was already used with another sequence",
            source=source,
            event_id=event["event_id"],
        )
    conn.execute(
        "INSERT INTO event_streams(source, committed_seq, watermark_clock_ms, last_event_id, "
        "updated_by_op) VALUES(?, ?, ?, ?, ?) "
        "ON CONFLICT(source) DO UPDATE SET committed_seq=excluded.committed_seq, "
        "watermark_clock_ms=excluded.watermark_clock_ms, last_event_id=excluded.last_event_id, "
        "updated_by_op=excluded.updated_by_op",
        (
            source,
            seq,
            max(int(stream["watermark_clock_ms"]), int(event["logical_clock_ms"])),
            event["event_id"],
            op_id,
        ),
    )
    conn.execute(
        "INSERT INTO event_commits(source, seq, event_id, logical_clock_ms, content_digest, "
        "status, reason, event_json, candidate_id, committed_by_op, committed_seq) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            source,
            seq,
            event["event_id"],
            event["logical_clock_ms"],
            event["content_digest"],
            status,
            reason,
            _json(event),
            candidate_id,
            op_id,
            seq,
        ),
    )
    conn.execute("UPDATE snapshot_state SET event_commits=event_commits+1 WHERE singleton=1")


def update_event_status(
    conn: sqlite3.Connection,
    *,
    source: str,
    seq: int,
    status: str,
    reason: str | None,
) -> None:
    conn.execute(
        "UPDATE event_commits SET status=?, reason=? WHERE source=? AND seq=?",
        (status, reason, source, seq),
    )


def list_event_commits(
    conn: sqlite3.Connection,
    *,
    source: str | None = None,
) -> list[dict[str, Any]]:
    if source is None:
        rows = conn.execute(
            "SELECT * FROM event_commits ORDER BY source, seq"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM event_commits WHERE source=? ORDER BY seq", (source,)
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["event"] = _loads(item.pop("event_json"), {})
        result.append(item)
    return result


def list_event_commits_by_op(
    conn: sqlite3.Connection,
    op_id: str,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM event_commits WHERE committed_by_op=? ORDER BY source, seq",
        (op_id,),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["event"] = _loads(item.pop("event_json"), {})
        result.append(item)
    return result


def list_streams(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in conn.execute("SELECT * FROM event_streams ORDER BY source").fetchall()
    ]


def insert_view(
    conn: sqlite3.Connection,
    *,
    view_version: str,
    clock_ms: int,
    payload: dict[str, Any],
    op_id: str,
) -> dict[str, Any]:
    payload_json = _json(payload)
    payload_hash = sha256_value(payload)
    row = conn.execute(
        "SELECT * FROM views WHERE view_version=?", (view_version,)
    ).fetchone()
    if row is not None:
        if row["payload_sha256"] != payload_hash:
            raise error(
                "GENERATION_CONFLICT",
                "view version reused with different payload",
                view_version=view_version,
            )
        return dict(row)
    created_seq = _view_seq(conn)
    conn.execute(
        "INSERT INTO views(view_version, clock_ms, payload_json, payload_sha256, created_by_op, "
        "created_seq) VALUES(?, ?, ?, ?, ?, ?)",
        (view_version, clock_ms, payload_json, payload_hash, op_id, created_seq),
    )
    row = conn.execute(
        "SELECT * FROM views WHERE view_version=?", (view_version,)
    ).fetchone()
    return dict(row)


def latest_view(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM views ORDER BY view_seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["payload"] = _loads(result.pop("payload_json"), {})
    return result


def list_views(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute("SELECT * FROM views ORDER BY view_seq").fetchall():
        item = dict(row)
        item["payload"] = _loads(item.pop("payload_json"), {})
        result.append(item)
    return result


def insert_published_recovery_snapshot(
    conn: sqlite3.Connection,
    *,
    view_version: str,
    view_seq: int,
    clock_ms: int,
    op_id: str,
) -> dict[str, Any]:
    watermarks = [
        {
            "committed_seq": int(row["committed_seq"]),
            "last_event_id": row["last_event_id"],
            "source": row["source"],
            "watermark_clock_ms": int(row["watermark_clock_ms"]),
        }
        for row in conn.execute("SELECT * FROM event_streams ORDER BY source").fetchall()
    ]
    migrations = list_migrations(conn)
    transitions = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM state_transitions WHERE view_version=? "
            "ORDER BY created_seq, transition_id",
            (view_version,),
        ).fetchall()
    ]
    payload = {
        "clock_ms": clock_ms,
        "event_watermarks": watermarks,
        "migration_decisions": migrations,
        "state_transitions": transitions,
        "view_seq": view_seq,
        "view_version": view_version,
    }
    publication_id = sha256_value(payload)
    conn.execute(
        "INSERT OR IGNORE INTO published_recovery_snapshots(publication_id, view_version, "
        "view_seq, clock_ms, event_watermarks_json, migration_decisions_json, "
        "state_transitions_json, op_id, created_seq) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            publication_id,
            view_version,
            view_seq,
            clock_ms,
            _json(watermarks),
            _json(migrations),
            _json(transitions),
            op_id,
            _transition_seq(conn),
        ),
    )
    row = conn.execute(
        "SELECT * FROM published_recovery_snapshots WHERE publication_id=?",
        (publication_id,),
    ).fetchone()
    result = dict(row)
    result["event_watermarks"] = _loads(result.pop("event_watermarks_json"), [])
    result["migration_decisions"] = _loads(result.pop("migration_decisions_json"), [])
    result["state_transitions"] = _loads(result.pop("state_transitions_json"), [])
    return result


def latest_published_recovery_snapshot(
    conn: sqlite3.Connection,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM published_recovery_snapshots ORDER BY created_seq DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["event_watermarks"] = _loads(result.pop("event_watermarks_json"), [])
    result["migration_decisions"] = _loads(result.pop("migration_decisions_json"), [])
    result["state_transitions"] = _loads(result.pop("state_transitions_json"), [])
    return result


def list_migrations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute(
        "SELECT * FROM migration_decisions ORDER BY service, environment, effective_at_ms"
    ).fetchall():
        item = dict(row)
        item["source_ref"] = _loads(item.pop("source_ref_json"), {})
        item["original"] = _loads(item.pop("original_json"), {})
        item["decision"] = _loads(item.pop("decision_json"), {})
        result.append(item)
    return result


def list_transitions(
    conn: sqlite3.Connection,
    *,
    target: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    if target is None:
        rows = conn.execute(
            "SELECT * FROM state_transitions ORDER BY created_seq, transition_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM state_transitions WHERE service=? AND environment=? "
            "ORDER BY created_seq, transition_id",
            target,
        ).fetchall()
    return [dict(row) for row in rows]


def insert_transition(
    conn: sqlite3.Connection,
    *,
    service: str,
    environment: str,
    from_state: str | None,
    to_state: str,
    previous_active_candidate_id: str | None,
    active_candidate_id: str | None,
    clock_ms: int,
    view_version: str,
    reason: str,
    op_id: str,
) -> None:
    transition_id = sha256_value(
        {
            "active_candidate_id": active_candidate_id,
            "clock_ms": clock_ms,
            "environment": environment,
            "from_state": from_state,
            "previous_active_candidate_id": previous_active_candidate_id,
            "service": service,
            "to_state": to_state,
            "view_version": view_version,
        }
    )
    conn.execute(
        "INSERT OR IGNORE INTO state_transitions(transition_id, service, environment, "
        "from_state, to_state, previous_active_candidate_id, active_candidate_id, clock_ms, "
        "view_version, reason, created_by_op, created_seq) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            transition_id,
            service,
            environment,
            from_state,
            to_state,
            previous_active_candidate_id,
            active_candidate_id,
            clock_ms,
            view_version,
            reason,
            op_id,
            _transition_seq(conn),
        ),
    )


def list_recovery_decisions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute(
        "SELECT * FROM recovery_decisions ORDER BY created_seq, recovery_id"
    ).fetchall():
        item = dict(row)
        item["details"] = _loads(item.pop("details_json"), {})
        result.append(item)
    return result


def list_operations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    result = []
    for row in conn.execute("SELECT * FROM operations ORDER BY created_seq").fetchall():
        item = dict(row)
        item["request"] = _loads(item.pop("request_json"), {})
        item["result"] = _loads(item.pop("result_json"))
        item["error"] = _loads(item.pop("error_json"))
        result.append(item)
    return result


def database_event_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT event_commits FROM snapshot_state WHERE singleton=1").fetchone()
    return int(row[0]) if row is not None else 0


def operation_id(kind: str, request_digest: str) -> str:
    return sha256_value({"kind": kind, "request_digest": request_digest})


def request_digest(request: dict[str, Any]) -> str:
    return sha256_value(request)


def maybe_crash(step_key: str) -> None:
    """Deterministic fault injection for SIGKILL-style recovery tests."""

    requested = os.environ.get("CONFIG_ENGINE_CRASH_AFTER_STEP", "")
    if requested and requested in {step_key, "*"}:
        os._exit(97)
