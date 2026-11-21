from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError
from .jsonutil import canonical_json
from .locking import FileLock
from .spec import WorkspaceSpec, validate_workspace


SCHEMA_VERSION = 1


SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE replicas (
    replica_id TEXT PRIMARY KEY,
    spec_json TEXT NOT NULL
);

CREATE TABLE requests (
    request_id INTEGER PRIMARY KEY,
    arrival_ms INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    service_class TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    status TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    batch_id TEXT,
    assigned_step INTEGER,
    status_clock_ms INTEGER
);

CREATE INDEX requests_status_idx ON requests(status, arrival_ms, request_id);

CREATE TABLE state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    clock_ms INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    queue_json TEXT NOT NULL,
    queue_count INTEGER NOT NULL,
    high_watermark INTEGER NOT NULL,
    cumulative_json TEXT NOT NULL
);

CREATE TABLE steps (
    step_index INTEGER PRIMARY KEY,
    clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL UNIQUE,
    status TEXT NOT NULL,
    queue_before INTEGER NOT NULL,
    queue_after INTEGER NOT NULL,
    arrivals_json TEXT NOT NULL,
    batch_id TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE batches (
    batch_id TEXT PRIMARY KEY,
    batch_seq INTEGER NOT NULL UNIQUE,
    replica_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    version INTEGER NOT NULL,
    status TEXT NOT NULL,
    request_ids_json TEXT NOT NULL,
    queue_before INTEGER NOT NULL,
    queue_after INTEGER NOT NULL,
    spectrum_payload_json TEXT NOT NULL,
    diagnostics_json TEXT NOT NULL
);

CREATE INDEX batches_step_idx ON batches(step_index, replica_id);

CREATE TABLE spectrum (
    batch_id TEXT NOT NULL,
    metric TEXT NOT NULL,
    status TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, metric),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
);

CREATE TABLE batch_requests (
    request_id INTEGER PRIMARY KEY,
    batch_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id) ON DELETE CASCADE
);

CREATE TABLE request_events (
    request_id INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    event_no INTEGER NOT NULL,
    clock_ms INTEGER NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    batch_id TEXT,
    PRIMARY KEY (request_id, step_index, event_no)
);
"""


def lock_path(db_path: str | os.PathLike[str]) -> Path:
    path = Path(db_path)
    return path.with_name(path.name + ".lock")


def connect(db_path: str | os.PathLike[str], *, create: bool = False) -> sqlite3.Connection:
    path = Path(db_path)
    if not create and not path.exists():
        raise BusinessError(
            "E_DB_NOT_FOUND", "state database does not exist", {"db": str(path)}
        )
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(path), timeout=30.0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = FULL")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield connection
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def _meta_set(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def meta_get(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return None if row is None else str(row["value"])


def _initialize_database(
    db_path: Path, workspace: WorkspaceSpec
) -> None:
    connection = connect(db_path, create=True)
    try:
        connection.executescript(SCHEMA_SQL)
        with transaction(connection):
            _meta_set(connection, "schema_version", str(SCHEMA_VERSION))
            _meta_set(connection, "sample_sha256", workspace.sample_hash)
            _meta_set(connection, "workspace_json", workspace.canonical())
            _meta_set(connection, "model_id", workspace.model.model_id)
            for replica in workspace.replicas:
                connection.execute(
                    "INSERT INTO replicas(replica_id, spec_json) VALUES(?, ?)",
                    (replica.replica_id, canonical_json(replica.as_dict())),
                )
            for request in workspace.requests:
                connection.execute(
                    """
                    INSERT INTO requests(
                        request_id, arrival_ms, deadline_ms, service_class,
                        input_tokens, output_tokens, status, outcome_code
                    ) VALUES(?, ?, ?, ?, ?, ?, 'queued', 'OK_QUEUED')
                    """,
                    (
                        request.request_id,
                        request.arrival_ms,
                        request.deadline_ms,
                        request.service_class,
                        request.input_tokens,
                        request.output_tokens,
                    ),
                )
            initial_cumulative = {replica.replica_id: {} for replica in workspace.replicas}
            connection.execute(
                """
                INSERT INTO state(
                    id, clock_ms, step_index, version, queue_json,
                    queue_count, high_watermark, cumulative_json
                ) VALUES(1, ?, 0, 0, '[]', 0, 0, ?)
                """,
                (
                    workspace.precision.start_clock_ms,
                    canonical_json(initial_cumulative),
                ),
            )
    finally:
        connection.close()


def load_database(
    db_path: str | os.PathLike[str], workspace: WorkspaceSpec, *, force: bool = False
) -> dict[str, Any]:
    """Create a fresh database only after WorkspaceSpec validation succeeded."""
    destination = Path(db_path)
    with FileLock(lock_path(destination)):
        if destination.exists() and not force:
            raise BusinessError(
                "E_STATE_EXISTS",
                "state database already exists; pass --force to replace it",
                {"db": str(destination)},
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink()
            _initialize_database(temporary, workspace)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(destination) + suffix)
                try:
                    sidecar.unlink()
                except FileNotFoundError:
                    pass
            os.replace(temporary, destination)
        finally:
            for candidate in (
                temporary,
                Path(str(temporary) + "-wal"),
                Path(str(temporary) + "-shm"),
            ):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
    return {
        "db": str(destination),
        "sample_sha256": workspace.sample_hash,
        "model_id": workspace.model.model_id,
        "request_count": len(workspace.requests),
        "replica_count": len(workspace.replicas),
    }


def workspace_from_db(connection: sqlite3.Connection) -> WorkspaceSpec:
    raw = meta_get(connection, "workspace_json")
    if raw is None:
        raise BusinessError("E_DB_SCHEMA", "workspace metadata is missing")
    try:
        data = json.loads(raw)
        workspace = validate_workspace(
            data["model"], data["replicas"], data["requests"], data["precision"]
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BusinessError("E_DB_SCHEMA", "workspace metadata is malformed") from exc
    expected = meta_get(connection, "sample_sha256")
    if expected != workspace.sample_hash:
        raise BusinessError(
            "E_STATE_MISMATCH",
            "workspace metadata hash does not match its content",
            {"expected": expected, "actual": workspace.sample_hash},
            exit_code=3,
        )
    return workspace


def state_get(connection: sqlite3.Connection) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
    if row is None:
        raise BusinessError("E_DB_SCHEMA", "state row is missing")
    return {
        "clock_ms": int(row["clock_ms"]),
        "step_index": int(row["step_index"]),
        "version": int(row["version"]),
        "queue": json.loads(row["queue_json"]),
        "queue_count": int(row["queue_count"]),
        "high_watermark": int(row["high_watermark"]),
        "cumulative": json.loads(row["cumulative_json"]),
    }


def state_set(
    connection: sqlite3.Connection,
    *,
    clock_ms: int,
    step_index: int,
    version: int,
    queue: list[int],
    high_watermark: int,
    cumulative: dict[str, Any],
) -> None:
    cursor = connection.execute(
        """
        UPDATE state
        SET clock_ms = ?, step_index = ?, version = ?, queue_json = ?,
            queue_count = ?, high_watermark = ?, cumulative_json = ?
        WHERE id = 1
        """,
        (
            clock_ms,
            step_index,
            version,
            canonical_json(queue),
            len(queue),
            high_watermark,
            canonical_json(cumulative),
        ),
    )
    if cursor.rowcount != 1:
        raise BusinessError("E_STATE_CONFLICT", "failed to update the singleton state")


def request_rows(connection: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = connection.execute("SELECT * FROM requests ORDER BY request_id").fetchall()
    return {
        int(row["request_id"]): {
            "request_id": int(row["request_id"]),
            "arrival_ms": int(row["arrival_ms"]),
            "deadline_ms": int(row["deadline_ms"]),
            "service_class": str(row["service_class"]),
            "input_tokens": int(row["input_tokens"]),
            "output_tokens": int(row["output_tokens"]),
            "status": str(row["status"]),
            "outcome_code": str(row["outcome_code"]),
            "batch_id": row["batch_id"],
            "assigned_step": row["assigned_step"],
            "status_clock_ms": row["status_clock_ms"],
        }
        for row in rows
    }


def set_request_status(
    connection: sqlite3.Connection,
    request_id: int,
    *,
    from_status: str,
    to_status: str,
    outcome_code: str,
    step_index: int,
    clock_ms: int,
    batch_id: str | None = None,
    event_no: int = 1,
) -> None:
    cursor = connection.execute(
        """
        UPDATE requests
        SET status = ?, outcome_code = ?, batch_id = COALESCE(?, batch_id),
            assigned_step = COALESCE(?, assigned_step), status_clock_ms = ?
        WHERE request_id = ? AND status = ?
        """,
        (
            to_status,
            outcome_code,
            batch_id,
            step_index if batch_id is not None else None,
            clock_ms,
            request_id,
            from_status,
        ),
    )
    if cursor.rowcount != 1:
        row = connection.execute(
            "SELECT status, batch_id FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is None:
            raise BusinessError(
                "E_DB_SCHEMA", "request row is missing", {"request_id": request_id}
            )
        if row["status"] == to_status and (
            batch_id is None or row["batch_id"] == batch_id
        ):
            return
        raise BusinessError(
            "E_STATE_CONFLICT",
            "request status transition conflicted with persisted state",
            {
                "request_id": request_id,
                "expected_from": from_status,
                "actual": row["status"],
                "expected_to": to_status,
            },
        )
    connection.execute(
        """
        INSERT INTO request_events(
            request_id, step_index, event_no, clock_ms, from_status, to_status,
            outcome_code, batch_id
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(request_id, step_index, event_no) DO UPDATE SET
            to_status = excluded.to_status,
            outcome_code = excluded.outcome_code,
            batch_id = excluded.batch_id
        """,
        (
            request_id,
            step_index,
            event_no,
            clock_ms,
            from_status,
            to_status,
            outcome_code,
            batch_id,
        ),
    )


def database_summary(connection: sqlite3.Connection) -> dict[str, Any]:
    state = state_get(connection)
    rows = request_rows(connection)
    counts: dict[str, int] = {}
    for row in rows.values():
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    batches = connection.execute("SELECT COUNT(*) AS n FROM batches").fetchone()
    spectrum_rows = connection.execute("SELECT COUNT(*) AS n FROM spectrum").fetchone()
    return {
        "clock_ms": state["clock_ms"],
        "step_index": state["step_index"],
        "version": state["version"],
        "queue_count": state["queue_count"],
        "queue": state["queue"],
        "high_watermark": state["high_watermark"],
        "request_status_counts": dict(sorted(counts.items())),
        "committed_batches": int(batches["n"]),
        "spectrum_entries": int(spectrum_rows["n"]),
        "cumulative": state["cumulative"],
    }


def clone_database(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    source_conn = connect(source)
    try:
        target_conn = sqlite3.connect(str(destination), isolation_level=None)
        try:
            source_conn.backup(target_conn)
            target_conn.execute("PRAGMA journal_mode = WAL")
        finally:
            target_conn.close()
    finally:
        source_conn.close()
