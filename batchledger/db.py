"""SQLite persistence, locking and idempotent transactional commits."""

from __future__ import annotations

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, ROUND_FLOOR
from pathlib import Path
from typing import Any, Iterator

from .errors import BusinessError, StateError
from .fixture import canonical_json, validate_and_build
from .models import Fixture, Route
from .planner import BatchPlan, STATE_VERSION, WindowPlan, route_requests

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE replicas (
    replica_id TEXT PRIMARY KEY,
    compute_capacity REAL NOT NULL,
    memory_limit_mib REAL NOT NULL,
    window_ms INTEGER NOT NULL,
    max_batch_size INTEGER NOT NULL,
    max_sequence_length INTEGER NOT NULL,
    ordinal INTEGER NOT NULL
);

CREATE TABLE requests (
    request_id INTEGER PRIMARY KEY,
    arrival_ms INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    planned_replica_id TEXT NOT NULL,
    planned_window_index INTEGER NOT NULL,
    planned_window_start_ms INTEGER NOT NULL,
    planned_window_end_ms INTEGER NOT NULL,
    version INTEGER NOT NULL,
    route_precision_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('PENDING','BATCHED','RUNNING','COMPLETED','EXPIRED','FAILED')
    ),
    status_reason TEXT NOT NULL,
    batch_index INTEGER,
    updated_clock_ms INTEGER NOT NULL
);

CREATE TABLE windows (
    replica_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PLANNED','OPEN','COMMITTED')),
    plan_hash TEXT,
    decision_json TEXT,
    error_spectrum_json TEXT,
    precision_events_json TEXT,
    precision_sensitive INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (replica_id, window_index, version)
);

CREATE TABLE batches (
    replica_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    request_ids_json TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    finish_ms INTEGER NOT NULL,
    metrics_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    error_spectrum_json TEXT NOT NULL,
    precision_events_json TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL,
    plan_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PLANNED','RUNNING','FINISHED','FAILED')),
    PRIMARY KEY (replica_id, window_index, version, batch_index)
);

CREATE TABLE assignments (
    request_id INTEGER PRIMARY KEY,
    replica_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    compute_fixed INTEGER NOT NULL,
    compute_scale TEXT NOT NULL,
    memory_fixed INTEGER NOT NULL,
    memory_scale TEXT NOT NULL,
    UNIQUE (replica_id, window_index, version, batch_index, request_id)
);

CREATE TABLE window_decisions (
    replica_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    request_id INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    reason TEXT NOT NULL,
    batch_index INTEGER,
    detail_json TEXT NOT NULL,
    PRIMARY KEY (replica_id, window_index, version, request_id)
);

CREATE TABLE ledger_entries (
    entry_id TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL,
    window_index INTEGER NOT NULL,
    version INTEGER NOT NULL,
    batch_index INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX ix_ledger_batch
ON ledger_entries(replica_id, window_index, version, batch_index, ordinal);

CREATE TABLE injections (
    injection_id TEXT PRIMARY KEY,
    replica_id TEXT,
    window_index INTEGER NOT NULL,
    phase TEXT NOT NULL CHECK (
        phase IN (
            'window_open',
            'batch_adjudication',
            'batch_commit',
            'window_commit',
            'batch_execute'
        )
    ),
    batch_index INTEGER,
    signal TEXT NOT NULL CHECK (signal = 'SIGKILL'),
    enabled INTEGER NOT NULL,
    fired INTEGER NOT NULL,
    sequence INTEGER NOT NULL
);

CREATE TABLE lifecycle (
    request_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    logical_clock_ms INTEGER NOT NULL,
    PRIMARY KEY (request_id, seq)
);

CREATE TABLE replica_state (
    replica_id TEXT PRIMARY KEY,
    available_ms INTEGER NOT NULL,
    committed_windows INTEGER NOT NULL
);
"""


class FileLock:
    """Small fcntl-based advisory lock, independent from SQLite transactions."""

    def __init__(self, path: str | Path, *, shared: bool = False) -> None:
        self.path = Path(path)
        self.shared = shared
        self._handle: Any = None

    def __enter__(self) -> "FileLock":
        self._handle = open(self.path, "a+", encoding="utf-8")
        operation = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
        fcntl.flock(self._handle.fileno(), operation)
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=60.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _json(value: Any) -> str:
    return canonical_json(value)


def _loads(value: str | None) -> Any:
    if value is None:
        return None
    return json.loads(value)


def _fixed_limit(limit: float, scale: str | float) -> int:
    return int(
        (Decimal(str(float(limit))) * Decimal(str(float(scale)))).to_integral_value(
            rounding=ROUND_FLOOR
        )
    )


class LedgerDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.conn: sqlite3.Connection | None = None

    @property
    def lock_path(self) -> Path:
        return Path(str(self.path) + ".lock")

    def lock(self, *, shared: bool = False) -> FileLock:
        return FileLock(self.lock_path, shared=shared)

    def open(self) -> "LedgerDB":
        if not self.path.exists():
            raise StateError(
                "E_STATE_NOT_FOUND", f"state database does not exist: {self.path}"
            )
        self.conn = _connect(self.path)
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA synchronous = FULL")
        return self

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def __enter__(self) -> "LedgerDB":
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        assert self.conn is not None
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def meta_get(self, key: str, default: str | None = None) -> str | None:
        assert self.conn is not None
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def meta_set(self, key: str, value: str) -> None:
        assert self.conn is not None
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    def fixture(self) -> Fixture:
        raw = self.meta_get("fixture_json")
        if raw is None:
            raise StateError("E_STATE_INVALID", "state database has no fixture snapshot")
        try:
            bundle = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StateError(
                "E_STATE_INVALID", "persisted fixture snapshot is not valid JSON"
            ) from exc
        fixture_hash = self.meta_get("fixture_hash")
        try:
            fixture = validate_and_build(
                bundle,
                source=self.meta_get("fixture_source", "<database>") or "<database>",
            )
        except BusinessError as exc:
            raise StateError(
                "E_STATE_INVALID",
                "persisted fixture snapshot is invalid",
                details=exc.details,
            ) from exc
        if fixture_hash != fixture.content_hash:
            raise StateError(
                "E_STATE_INVALID",
                "persisted fixture hash does not match canonical fixture content",
            )
        return fixture

    @staticmethod
    def initialize(
        path: str | Path,
        fixture: Fixture,
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        """Create or idempotently verify a state database without partial state."""

        db_path = Path(path).expanduser()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(db_path) + ".lock")
        with lock:
            if db_path.exists() and not replace:
                database: LedgerDB | None = None
                try:
                    database = LedgerDB(db_path).open()
                    try:
                        existing_hash = database.meta_get("fixture_hash")
                    except sqlite3.DatabaseError:
                        existing_hash = None
                except sqlite3.DatabaseError:
                    existing_hash = None
                finally:
                    if database is not None:
                        database.close()
                if existing_hash == fixture.content_hash:
                    return {
                        "created": False,
                        "already_loaded": True,
                        "fixture_hash": fixture.content_hash,
                        "database": str(db_path),
                    }
                if existing_hash is None:
                    raise StateError(
                        "E_STATE_CONFLICT",
                        "target database exists but is not a batchledger state database",
                    )
                raise StateError(
                    "E_STATE_CONFLICT",
                    "target database contains a different fixture; use --reset",
                    details=[
                        {
                            "existing_fixture_hash": existing_hash,
                            "requested_fixture_hash": fixture.content_hash,
                        }
                    ],
                )

            temp_path = db_path.parent / f".{db_path.name}.init.{os.getpid()}.tmp"
            for candidate in (temp_path, Path(str(temp_path) + "-wal"), Path(str(temp_path) + "-shm")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            conn = _connect(temp_path)
            try:
                conn.execute("PRAGMA journal_mode = DELETE")
                conn.execute("PRAGMA synchronous = FULL")
                conn.executescript(SCHEMA_SQL)
                routes = route_requests(fixture)
                conn.execute("BEGIN IMMEDIATE")
                meta = {
                    "schema_version": str(fixture.schema_version),
                    "state_version": str(STATE_VERSION),
                    "fixture_hash": fixture.content_hash,
                    "fixture_json": canonical_json(fixture.canonical),
                    "fixture_source": fixture.source,
                    "logical_clock_ms": "0",
                    "complete": "0",
                    "last_injection_id": "",
                }
                conn.executemany(
                    "INSERT INTO meta(key, value) VALUES(?, ?)",
                    sorted(meta.items()),
                )
                conn.executemany(
                    "INSERT INTO replicas VALUES(?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            replica.replica_id,
                            replica.compute_capacity,
                            replica.memory_limit_mib,
                            replica.window_ms,
                            replica.max_batch_size,
                            replica.max_sequence_length,
                            ordinal,
                        )
                        for ordinal, replica in enumerate(fixture.replicas)
                    ],
                )
                route_map = {route.request_id: route for route in routes}
                route_precision = {
                    route.request_id: list(route.precision_events) for route in routes
                }
                conn.executemany(
                    """
                    INSERT INTO requests(
                        request_id, arrival_ms, priority, input_tokens,
                        max_output_tokens, deadline_ms, planned_replica_id,
                        planned_window_index, planned_window_start_ms,
                        planned_window_end_ms, version, route_precision_json,
                        status, status_reason, batch_index, updated_clock_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', 'loaded', NULL, 0)
                    """,
                    [
                        (
                            request.request_id,
                            request.arrival_ms,
                            request.priority,
                            request.input_tokens,
                            request.max_output_tokens,
                            request.deadline_ms,
                            route_map[request.request_id].replica_id,
                            route_map[request.request_id].window_index,
                            route_map[request.request_id].window_start_ms,
                            route_map[request.request_id].window_end_ms,
                            route_map[request.request_id].version,
                            _json(route_precision[request.request_id]),
                        )
                        for request in fixture.requests
                    ],
                )
                windows: dict[tuple[str, int, int], tuple[int, int]] = {}
                for route in routes:
                    windows[
                        (route.replica_id, route.window_index, route.version)
                    ] = (route.window_start_ms, route.window_end_ms)
                conn.executemany(
                    """
                    INSERT INTO windows(
                        replica_id, window_index, version, start_ms, end_ms,
                        status, plan_hash, decision_json, error_spectrum_json,
                        precision_events_json, precision_sensitive
                    ) VALUES(?, ?, ?, ?, ?, 'PLANNED', NULL, NULL, NULL, NULL, 0)
                    """,
                    [
                        (replica_id, window_index, version, start_ms, end_ms)
                        for (replica_id, window_index, version), (
                            start_ms,
                            end_ms,
                        ) in sorted(
                            windows.items(),
                            key=lambda item: (
                                item[1][0],
                                item[0][0],
                                item[0][1],
                            ),
                        )
                    ],
                )
                conn.executemany(
                    "INSERT INTO replica_state VALUES(?, 0, 0)",
                    [(replica.replica_id,) for replica in fixture.replicas],
                )
                conn.executemany(
                    "INSERT INTO lifecycle VALUES(?, ?, NULL, 'PENDING', 'loaded', 0)",
                    [(request.request_id, 1) for request in fixture.requests],
                )
                conn.executemany(
                    """
                    INSERT INTO injections(
                        injection_id, replica_id, window_index, phase,
                        batch_index, signal, enabled, fired, sequence
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    [
                        (
                            injection.injection_id,
                            injection.replica_id,
                            injection.window_index,
                            injection.phase,
                            injection.batch_index,
                            injection.signal_name,
                            1 if injection.enabled else 0,
                            injection.sequence,
                        )
                        for injection in fixture.injections
                    ],
                )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                conn.close()
                for candidate in (
                    temp_path,
                    Path(str(temp_path) + "-wal"),
                    Path(str(temp_path) + "-shm"),
                ):
                    try:
                        candidate.unlink()
                    except FileNotFoundError:
                        pass
                raise
            else:
                conn.close()
            for suffix in ("", "-wal", "-shm"):
                try:
                    Path(str(db_path) + suffix).unlink()
                except FileNotFoundError:
                    pass
            os.replace(temp_path, db_path)
            final_connection = _connect(db_path)
            try:
                final_connection.execute("PRAGMA journal_mode = WAL")
                final_connection.execute("PRAGMA synchronous = FULL")
            finally:
                final_connection.close()
            return {
                "created": True,
                "already_loaded": False,
                "fixture_hash": fixture.content_hash,
                "database": str(db_path),
                "routes": [
                    {
                        "request_id": route.request_id,
                        "replica_id": route.replica_id,
                        "window_index": route.window_index,
                    }
                    for route in routes
                ],
            }

    def requests(self) -> list[sqlite3.Row]:
        assert self.conn is not None
        return list(
            self.conn.execute(
                "SELECT * FROM requests ORDER BY request_id"
            ).fetchall()
        )

    def request_by_id(self, request_id: int) -> sqlite3.Row | None:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()

    def routes(self) -> dict[int, Route]:
        routes: dict[int, Route] = {}
        for row in self.requests():
            routes[row["request_id"]] = Route(
                request_id=row["request_id"],
                replica_id=row["planned_replica_id"],
                window_index=row["planned_window_index"],
                window_start_ms=row["planned_window_start_ms"],
                window_end_ms=row["planned_window_end_ms"],
                version=row["version"],
                precision_events=tuple(_loads(row["route_precision_json"]) or []),
            )
        return routes

    def windows(self) -> list[sqlite3.Row]:
        assert self.conn is not None
        return list(
            self.conn.execute(
                "SELECT * FROM windows "
                "ORDER BY start_ms, replica_id, window_index, version"
            ).fetchall()
        )

    def window(
        self, replica_id: str, window_index: int, version: int
    ) -> sqlite3.Row | None:
        assert self.conn is not None
        return self.conn.execute(
            "SELECT * FROM windows "
            "WHERE replica_id = ? AND window_index = ? AND version = ?",
            (replica_id, window_index, version),
        ).fetchone()

    def window_requests(
        self, replica_id: str, window_index: int, version: int
    ) -> list[sqlite3.Row]:
        assert self.conn is not None
        return list(
            self.conn.execute(
                "SELECT * FROM requests "
                "WHERE planned_replica_id = ? AND planned_window_index = ? AND version = ? "
                "ORDER BY request_id",
                (replica_id, window_index, version),
            ).fetchall()
        )

    def replica_available_ms(self, replica_id: str) -> int:
        assert self.conn is not None
        row = self.conn.execute(
            "SELECT available_ms FROM replica_state WHERE replica_id = ?",
            (replica_id,),
        ).fetchone()
        if row is None:
            raise StateError(
                "E_STATE_INVALID", f"missing replica state for {replica_id}"
            )
        return int(row["available_ms"])

    def open_window(self, plan: WindowPlan) -> bool:
        assert self.conn is not None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status FROM windows "
                "WHERE replica_id = ? AND window_index = ? AND version = ?",
                (plan.replica_id, plan.window_index, plan.version),
            ).fetchone()
            if row is None:
                raise StateError(
                    "E_STATE_INVALID",
                    f"window not routed: {plan.replica_id}/{plan.window_index}",
                )
            if row["status"] == "COMMITTED":
                return False
            if row["status"] == "PLANNED":
                conn.execute(
                    "UPDATE windows SET status = 'OPEN' "
                    "WHERE replica_id = ? AND window_index = ? AND version = ?",
                    (plan.replica_id, plan.window_index, plan.version),
                )
            return True

    def _next_lifecycle_seq(self, conn: sqlite3.Connection, request_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq "
            "FROM lifecycle WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        return int(row["next_seq"])

    def _transition_request(
        self,
        conn: sqlite3.Connection,
        request_id: int,
        *,
        expected: tuple[str, ...],
        target: str,
        reason: str,
        clock_ms: int,
        batch_index: int | None = None,
    ) -> bool:
        row = conn.execute(
            "SELECT status, batch_index FROM requests WHERE request_id = ?",
            (request_id,),
        ).fetchone()
        if row is None:
            raise StateError("E_STATE_INVALID", f"unknown request {request_id}")
        if row["status"] == target:
            return False
        if row["status"] not in expected:
            raise StateError(
                "E_STATE_CONFLICT",
                f"request {request_id} cannot transition {row['status']} -> {target}",
            )
        conn.execute(
            "UPDATE requests SET status = ?, status_reason = ?, batch_index = ?, "
            "updated_clock_ms = ? "
            "WHERE request_id = ?",
            (target, reason, batch_index, clock_ms, request_id),
        )
        conn.execute(
            "INSERT INTO lifecycle VALUES(?, ?, ?, ?, ?, ?)",
            (
                request_id,
                self._next_lifecycle_seq(conn, request_id),
                row["status"],
                target,
                reason,
                clock_ms,
            ),
        )
        return True

    def commit_batch(
        self,
        plan: BatchPlan,
        window_end_ms: int,
        *,
        replica_compute_limit: float,
        replica_memory_limit: float,
        max_batch_size: int,
        compute_scale: float,
        memory_scale: float,
    ) -> dict[str, Any]:
        assert self.conn is not None
        with self.transaction() as conn:
            window_row = conn.execute(
                "SELECT status, end_ms FROM windows "
                "WHERE replica_id = ? AND window_index = ? AND version = ?",
                (plan.replica_id, plan.window_index, plan.version),
            ).fetchone()
            if window_row is None:
                raise StateError("E_STATE_INVALID", "cannot commit an unrouted window")
            if window_row["status"] == "COMMITTED":
                raise StateError(
                    "E_STATE_CONFLICT",
                    "cannot append a batch to a committed window",
                )
            existing = conn.execute(
                "SELECT plan_hash FROM batches "
                "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                (
                    plan.replica_id,
                    plan.window_index,
                    plan.version,
                    plan.batch_index,
                ),
            ).fetchone()
            if existing is not None:
                if existing["plan_hash"] != plan.plan_hash:
                    raise StateError(
                        "E_IDEMPOTENCY_CONFLICT",
                        "same batch identity was submitted with different content",
                    )
                return {"committed": False, "idempotent": True}

            if len(plan.request_ids) != len(set(plan.request_ids)):
                raise StateError(
                    "E_DUPLICATE_ASSIGNMENT",
                    "batch plan contains the same request more than once",
                )
            if not plan.request_ids:
                raise StateError("E_BATCH_INVALID", "empty batches are not committable")
            if plan.start_ms < int(window_row["end_ms"]):
                raise StateError(
                    "E_WINDOW_CLOCK_INVALID",
                    "batch cannot start before its logical window closes",
                )
            if len(plan.request_ids) > max_batch_size:
                raise StateError(
                    "E_BUDGET_OVERSOLD",
                    "batch exceeds replica max_batch_size",
                )
            compute = plan.metrics["compute"]
            memory = plan.metrics["memory"]
            compute_limit_fixed = _fixed_limit(replica_compute_limit, compute_scale)
            memory_limit_fixed = _fixed_limit(replica_memory_limit, memory_scale)
            if (
                compute["raw_float"] > replica_compute_limit
                or compute["fixed_value"] > compute_limit_fixed
            ):
                raise StateError("E_BUDGET_OVERSOLD", "batch subtracts too much compute")
            if (
                memory["raw_float"] > replica_memory_limit
                or memory["fixed_value"] > memory_limit_fixed
            ):
                raise StateError("E_BUDGET_OVERSOLD", "batch exceeds memory budget")

            for request_id in plan.request_ids:
                request_row = conn.execute(
                    "SELECT * FROM requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                if request_row is None:
                    raise StateError("E_STATE_INVALID", f"unknown request {request_id}")
                if (
                    request_row["planned_replica_id"] != plan.replica_id
                    or request_row["planned_window_index"] != plan.window_index
                    or request_row["version"] != plan.version
                ):
                    raise StateError(
                        "E_ROUTE_CONFLICT",
                        f"request {request_id} does not belong to committed window",
                    )
                if request_row["status"] == "EXPIRED":
                    raise StateError(
                        "E_EXPIRED_REINSERT",
                        f"expired request {request_id} cannot be re-batched",
                    )
                if plan.finish_ms > int(request_row["deadline_ms"]):
                    raise StateError(
                        "E_DEADLINE_VIOLATION",
                        f"batch completion exceeds deadline of request {request_id}",
                    )
                assignment = conn.execute(
                    "SELECT * FROM assignments WHERE request_id = ?", (request_id,)
                ).fetchone()
                if assignment is not None:
                    same = (
                        assignment["replica_id"] == plan.replica_id
                        and assignment["window_index"] == plan.window_index
                        and assignment["version"] == plan.version
                        and assignment["batch_index"] == plan.batch_index
                    )
                    if not same:
                        raise StateError(
                            "E_DUPLICATE_ASSIGNMENT",
                            f"request {request_id} already belongs to another batch",
                        )

            conn.execute(
                """
                INSERT INTO batches(
                    replica_id, window_index, version, batch_index,
                    request_ids_json, start_ms, finish_ms, metrics_json,
                    decision_json, error_spectrum_json, precision_events_json,
                    precision_sensitive, plan_hash, status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PLANNED')
                """,
                (
                    plan.replica_id,
                    plan.window_index,
                    plan.version,
                    plan.batch_index,
                    _json(list(plan.request_ids)),
                    plan.start_ms,
                    plan.finish_ms,
                    _json(plan.metrics),
                    _json(plan.decision),
                    _json(list(plan.error_spectrum)),
                    _json(list(plan.precision_events)),
                    1 if plan.precision_sensitive else 0,
                    plan.plan_hash,
                ),
            )
            for ordinal, entry in enumerate(plan.error_spectrum):
                conn.execute(
                    "INSERT INTO ledger_entries VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (
                        entry["entry_id"],
                        plan.replica_id,
                        plan.window_index,
                        plan.version,
                        plan.batch_index,
                        ordinal,
                        _json(entry),
                    ),
                )
            observations = {
                item["request_id"]: item
                for item in plan.decision.get("observations", [])
            }
            for request_id in plan.request_ids:
                observation = observations[request_id]
                conn.execute(
                    "INSERT INTO assignments VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        request_id,
                        plan.replica_id,
                        plan.window_index,
                        plan.version,
                        plan.batch_index,
                        observation["compute_fixed"],
                        observation["compute_scale"],
                        observation["memory_fixed"],
                        observation["memory_scale"],
                    ),
                )
                self._transition_request(
                    conn,
                    request_id,
                    expected=("PENDING",),
                    target="BATCHED",
                    reason="window_adjudication",
                    clock_ms=window_end_ms,
                    batch_index=plan.batch_index,
                )
                conn.execute(
                    """
                    INSERT INTO window_decisions VALUES(?, ?, ?, ?, 'BATCHED', ?, ?, ?)
                    ON CONFLICT(replica_id, window_index, version, request_id)
                    DO UPDATE SET
                        outcome = excluded.outcome,
                        reason = excluded.reason,
                        batch_index = excluded.batch_index,
                        detail_json = excluded.detail_json
                    """,
                    (
                        plan.replica_id,
                        plan.window_index,
                        plan.version,
                        request_id,
                        "window_adjudication",
                        plan.batch_index,
                        _json(
                            {
                                "batch_key": plan.batch_key,
                                "observations": observation,
                            }
                        ),
                    ),
                )
            return {"committed": True, "idempotent": False}

    def commit_expirations(self, plan: WindowPlan) -> int:
        assert self.conn is not None
        inserted = 0
        with self.transaction() as conn:
            for expired in plan.expired:
                request_id = expired["request_id"]
                row = conn.execute(
                    "SELECT status FROM requests WHERE request_id = ?", (request_id,)
                ).fetchone()
                if row is None:
                    raise StateError("E_STATE_INVALID", f"unknown request {request_id}")
                assignment = conn.execute(
                    "SELECT request_id FROM assignments WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if assignment is not None:
                    raise StateError(
                        "E_EXPIRED_AFTER_ASSIGNMENT",
                        f"request {request_id} was already assigned before expiry",
                    )
                if row["status"] == "PENDING":
                    self._transition_request(
                        conn,
                        request_id,
                        expected=("PENDING",),
                        target="EXPIRED",
                        reason=expired["reason"],
                        clock_ms=plan.end_ms,
                        batch_index=None,
                    )
                    inserted += 1
                elif row["status"] != "EXPIRED":
                    raise StateError(
                        "E_STATE_CONFLICT",
                        f"request {request_id} cannot expire from {row['status']}",
                    )
                conn.execute(
                    """
                    INSERT INTO window_decisions VALUES(?, ?, ?, ?, 'EXPIRED', ?, NULL, ?)
                    ON CONFLICT(replica_id, window_index, version, request_id)
                    DO NOTHING
                    """,
                    (
                        plan.replica_id,
                        plan.window_index,
                        plan.version,
                        request_id,
                        expired["reason"],
                        _json(expired),
                    ),
                )
        return inserted

    def commit_window(self, plan: WindowPlan) -> dict[str, Any]:
        assert self.conn is not None
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT status, plan_hash FROM windows "
                "WHERE replica_id = ? AND window_index = ? AND version = ?",
                (plan.replica_id, plan.window_index, plan.version),
            ).fetchone()
            if row is None:
                raise StateError("E_STATE_INVALID", "window not found")
            if row["status"] == "COMMITTED":
                if row["plan_hash"] != plan.plan_hash:
                    raise StateError(
                        "E_IDEMPOTENCY_CONFLICT",
                        "committed window has a different plan hash",
                    )
                return {"committed": False, "idempotent": True}
            persisted_batch_indexes = {
                int(item["batch_index"])
                for item in conn.execute(
                    "SELECT batch_index FROM batches "
                    "WHERE replica_id = ? AND window_index = ? AND version = ?",
                    (plan.replica_id, plan.window_index, plan.version),
                ).fetchall()
            }
            planned_batch_indexes = {
                batch.batch_index for batch in plan.batches
            }
            if persisted_batch_indexes != planned_batch_indexes:
                raise StateError(
                    "E_HALF_COMMIT",
                    "persisted batch set does not match deterministic window plan",
                )
            for batch in plan.batches:
                batch_row = conn.execute(
                    "SELECT plan_hash FROM batches "
                    "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                    (
                        plan.replica_id,
                        plan.window_index,
                        plan.version,
                        batch.batch_index,
                    ),
                ).fetchone()
                if batch_row is None:
                    raise StateError(
                        "E_HALF_COMMIT",
                        f"window is missing planned batch {batch.batch_index}",
                    )
                if batch_row["plan_hash"] != batch.plan_hash:
                    raise StateError(
                        "E_IDEMPOTENCY_CONFLICT",
                        "persisted batch differs from deterministic replay",
                    )
            for expired in plan.expired:
                decision = conn.execute(
                    "SELECT outcome FROM window_decisions "
                    "WHERE replica_id = ? AND window_index = ? AND version = ? AND request_id = ?",
                    (
                        plan.replica_id,
                        plan.window_index,
                        plan.version,
                        expired["request_id"],
                    ),
                ).fetchone()
                if decision is None or decision["outcome"] != "EXPIRED":
                    raise StateError(
                        "E_HALF_COMMIT",
                        f"window is missing expiry decision for {expired['request_id']}",
                    )
            conn.execute(
                """
                UPDATE windows SET
                    status = 'COMMITTED',
                    plan_hash = ?,
                    decision_json = ?,
                    error_spectrum_json = ?,
                    precision_events_json = ?,
                    precision_sensitive = ?
                WHERE replica_id = ? AND window_index = ? AND version = ?
                """,
                (
                    plan.plan_hash,
                    _json(plan.decision),
                    _json(
                        [
                            entry
                            for batch in plan.batches
                            for entry in batch.error_spectrum
                        ]
                    ),
                    _json(list(plan.precision_events)),
                    1 if plan.precision_sensitive else 0,
                    plan.replica_id,
                    plan.window_index,
                    plan.version,
                ),
            )
            available = conn.execute(
                "SELECT available_ms, committed_windows FROM replica_state "
                "WHERE replica_id = ?",
                (plan.replica_id,),
            ).fetchone()
            if available is None:
                raise StateError("E_STATE_INVALID", "missing replica state")
            max_finish = max(
                (batch.finish_ms for batch in plan.batches), default=plan.end_ms
            )
            new_available = max(int(available["available_ms"]), max_finish)
            conn.execute(
                "UPDATE replica_state SET available_ms = ?, committed_windows = ? "
                "WHERE replica_id = ?",
                (
                    new_available,
                    int(available["committed_windows"]) + 1,
                    plan.replica_id,
                ),
            )
            return {"committed": True, "idempotent": False}

    def start_batch_execution(self, batch_key: str) -> bool:
        assert self.conn is not None
        with self.transaction() as conn:
            identity = self._batch_identity(conn, batch_key)
            row = conn.execute(
                "SELECT status, finish_ms FROM batches "
                "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                identity,
            ).fetchone()
            if row is None:
                raise StateError("E_STATE_INVALID", f"unknown batch {batch_key}")
            if row["status"] in ("RUNNING", "FINISHED"):
                return False
            if row["status"] != "PLANNED":
                raise StateError(
                    "E_STATE_CONFLICT", f"batch {batch_key} has status {row['status']}"
                )
            conn.execute(
                "UPDATE batches SET status = 'RUNNING' "
                "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                identity,
            )
            request_ids = _loads(
                conn.execute(
                    "SELECT request_ids_json FROM batches "
                    "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                    identity,
                ).fetchone()["request_ids_json"]
            )
            for request_id in request_ids:
                self._transition_request(
                    conn,
                    request_id,
                    expected=("BATCHED",),
                    target="RUNNING",
                    reason="batch_execution_started",
                    clock_ms=int(row["finish_ms"]),
                    batch_index=identity[3],
                )
            return True

    def finish_batch_execution(self, batch_key: str) -> bool:
        assert self.conn is not None
        with self.transaction() as conn:
            identity = self._batch_identity(conn, batch_key)
            row = conn.execute(
                "SELECT status, finish_ms, request_ids_json FROM batches "
                "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                identity,
            ).fetchone()
            if row is None:
                raise StateError("E_STATE_INVALID", f"unknown batch {batch_key}")
            if row["status"] == "FINISHED":
                return False
            if row["status"] not in ("RUNNING", "PLANNED"):
                raise StateError(
                    "E_STATE_CONFLICT", f"batch {batch_key} has status {row['status']}"
                )
            conn.execute(
                "UPDATE batches SET status = 'FINISHED' "
                "WHERE replica_id = ? AND window_index = ? AND version = ? AND batch_index = ?",
                identity,
            )
            for request_id in _loads(row["request_ids_json"]):
                self._transition_request(
                    conn,
                    request_id,
                    expected=("BATCHED", "RUNNING"),
                    target="COMPLETED",
                    reason="batch_execution_finished",
                    clock_ms=int(row["finish_ms"]),
                    batch_index=identity[3],
                )
            return True

    @staticmethod
    def _batch_identity(
        conn: sqlite3.Connection, batch_key: str
    ) -> tuple[str, int, int, int]:
        row = conn.execute(
            "SELECT replica_id, window_index, version, batch_index "
            "FROM batches WHERE "
            "replica_id || ':w' || printf('%08d', window_index) || ':v' || "
            "version || ':b' || printf('%08d', batch_index) = ?",
            (batch_key,),
        ).fetchone()
        if row is None:
            raise StateError("E_STATE_INVALID", f"unknown batch {batch_key}")
        return (
            row["replica_id"],
            int(row["window_index"]),
            int(row["version"]),
            int(row["batch_index"]),
        )

    def batches_for_window(
        self, replica_id: str, window_index: int, version: int
    ) -> list[sqlite3.Row]:
        assert self.conn is not None
        return list(
            self.conn.execute(
                "SELECT * FROM batches "
                "WHERE replica_id = ? AND window_index = ? AND version = ? "
                "ORDER BY batch_index",
                (replica_id, window_index, version),
            ).fetchall()
        )

    def all_batches(self) -> list[sqlite3.Row]:
        assert self.conn is not None
        return list(
            self.conn.execute(
                "SELECT * FROM batches "
                "ORDER BY replica_id, window_index, version, batch_index"
            ).fetchall()
        )

    def add_injection(
        self,
        *,
        injection_id: str,
        replica_id: str | None,
        window_index: int,
        phase: str,
        batch_index: int | None,
        signal_name: str = "SIGKILL",
        enabled: bool = True,
    ) -> dict[str, Any]:
        assert self.conn is not None
        with self.transaction() as conn:
            max_sequence = conn.execute(
                "SELECT COALESCE(MAX(sequence), -1) + 1 AS next_seq FROM injections"
            ).fetchone()["next_seq"]
            try:
                conn.execute(
                    """
                    INSERT INTO injections(
                        injection_id, replica_id, window_index, phase,
                        batch_index, signal, enabled, fired, sequence
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        injection_id,
                        replica_id,
                        window_index,
                        phase,
                        batch_index,
                        signal_name,
                        1 if enabled else 0,
                        int(max_sequence),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StateError(
                    "E_INJECTION_DUPLICATE",
                    f"injection already exists: {injection_id}",
                ) from exc
            return {
                "injection_id": injection_id,
                "replica_id": replica_id,
                "window_index": window_index,
                "phase": phase,
                "batch_index": batch_index,
                "signal": signal_name,
                "enabled": enabled,
                "sequence": int(max_sequence),
            }

    def pending_injection(
        self,
        *,
        replica_id: str,
        window_index: int,
        phase: str,
        batch_index: int | None,
    ) -> sqlite3.Row | None:
        assert self.conn is not None
        batch_clause = ""
        params: list[Any] = [replica_id, window_index, phase]
        if phase not in ("window_open", "window_commit"):
            batch_clause = "AND (batch_index IS NULL OR batch_index = ?)"
            params.append(batch_index)
        return self.conn.execute(
            f"""
            SELECT * FROM injections
            WHERE enabled = 1
              AND fired = 0
              AND (replica_id IS NULL OR replica_id = ?)
              AND window_index = ?
              AND phase = ?
              {batch_clause}
            ORDER BY sequence, injection_id
            LIMIT 1
            """,
            tuple(params),
        ).fetchone()

    def mark_injection_fired(self, injection_id: str, phase: str) -> None:
        assert self.conn is not None
        with self.transaction() as conn:
            conn.execute(
                "UPDATE injections SET fired = 1 WHERE injection_id = ?",
                (injection_id,),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_injection_id', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (injection_id,),
            )
            conn.execute(
                "INSERT INTO meta(key, value) VALUES('last_injection_phase', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (phase,),
            )

    def injections(self) -> list[sqlite3.Row]:
        assert self.conn is not None
        return list(
            self.conn.execute(
                "SELECT * FROM injections ORDER BY sequence, injection_id"
            ).fetchall()
        )

    def set_logical_clock(self, clock_ms: int) -> None:
        self.meta_set("logical_clock_ms", str(clock_ms))

    def refresh_completion(self) -> bool:
        assert self.conn is not None
        row = self.conn.execute(
            """
            SELECT
              (SELECT COUNT(*) FROM windows WHERE status != 'COMMITTED') AS open_windows,
              (SELECT COUNT(*) FROM batches WHERE status != 'FINISHED') AS open_batches,
              (SELECT COUNT(*) FROM requests
                 WHERE status IN ('PENDING','BATCHED','RUNNING')) AS open_requests
            """
        ).fetchone()
        complete = (
            int(row["open_windows"]) == 0
            and int(row["open_batches"]) == 0
            and int(row["open_requests"]) == 0
        )
        self.meta_set("complete", "1" if complete else "0")
        return complete
