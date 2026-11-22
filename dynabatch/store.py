"""SQLite persistence, WAL configuration, and transactional state ingestion."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import canonical_json
from .errors import (
    BusinessError,
    E_CONFIG_CONFLICT,
    E_DB_INTEGRITY,
    E_DB_NOT_INITIALIZED,
    E_REQUEST_CONFLICT,
    E_VERSION,
    E_VERSION_CONFLICT,
    E_VERSION_ROLLBACK,
)
from .locking import FileLock
from .models import (
    Bundle,
    ExecutionConfig,
    LayerSpec,
    ModelSpec,
    PrecisionConfig,
    ReplicaSpec,
    ReplicaVersion,
    RequestSpec,
    SchedulerConfig,
)


SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_versions (
    version INTEGER PRIMARY KEY,
    effective_at_ms INTEGER NOT NULL,
    replicas_json TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    source_digest TEXT NOT NULL,
    created_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    seq INTEGER PRIMARY KEY,
    arrival_ms INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    replica_id TEXT,
    replica_version INTEGER,
    window_id TEXT,
    batch_id TEXT,
    not_before_ms INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    failure_count INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    expiry_count INTEGER NOT NULL DEFAULT 0,
    updated_clock_ms INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS windows (
    window_id TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    status TEXT NOT NULL,
    batch_count INTEGER NOT NULL DEFAULT 0,
    used_compute_fixed INTEGER NOT NULL DEFAULT 0,
    used_memory_peak_fixed INTEGER NOT NULL DEFAULT 0,
    deferred_count INTEGER NOT NULL DEFAULT 0,
    decision_digest TEXT,
    decision_json TEXT,
    prepared_clock_ms INTEGER,
    committed_clock_ms INTEGER,
    UNIQUE(replica_id, replica_version, start_ms, end_ms)
);

CREATE TABLE IF NOT EXISTS request_assignments (
    request_seq INTEGER PRIMARY KEY REFERENCES requests(seq),
    window_id TEXT NOT NULL REFERENCES windows(window_id),
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    state TEXT NOT NULL,
    assigned_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS assignment_history (
    evidence_id TEXT PRIMARY KEY,
    request_seq INTEGER NOT NULL,
    window_id TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    action TEXT NOT NULL,
    clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL REFERENCES windows(window_id),
    ordinal INTEGER NOT NULL,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    request_count INTEGER NOT NULL,
    compute_raw REAL NOT NULL,
    memory_raw REAL NOT NULL,
    latency_raw REAL NOT NULL,
    compute_fixed INTEGER NOT NULL,
    memory_fixed INTEGER NOT NULL,
    latency_fixed INTEGER NOT NULL,
    compute_error_bound REAL NOT NULL,
    memory_error_bound REAL NOT NULL,
    latency_error_bound REAL NOT NULL,
    first_constraint TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL DEFAULT 0,
    decision_json TEXT NOT NULL,
    prepared_clock_ms INTEGER NOT NULL,
    committed_clock_ms INTEGER,
    execution_status TEXT NOT NULL DEFAULT 'pending',
    started_clock_ms INTEGER,
    finished_clock_ms INTEGER,
    failure_reason TEXT,
    UNIQUE(window_id, ordinal)
);

CREATE TABLE IF NOT EXISTS batch_members (
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    request_seq INTEGER NOT NULL REFERENCES requests(seq),
    replica_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY(batch_id, request_seq),
    UNIQUE(request_seq, replica_version)
);

CREATE TABLE IF NOT EXISTS error_operations (
    op_id TEXT PRIMARY KEY,
    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
    metric TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    operation TEXT NOT NULL,
    operand_a_raw REAL,
    operand_b_raw REAL,
    result_raw REAL NOT NULL,
    scale INTEGER,
    fixed_result INTEGER,
    rounding TEXT,
    propagated_error_bound REAL NOT NULL,
    cumulative_error_bound REAL NOT NULL,
    details_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precision_flags (
    flag_id TEXT PRIMARY KEY,
    window_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_a TEXT NOT NULL,
    subject_b TEXT NOT NULL,
    metric TEXT NOT NULL,
    raw_a REAL NOT NULL,
    raw_b REAL NOT NULL,
    difference REAL NOT NULL,
    tolerance REAL NOT NULL,
    depended INTEGER NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_ledger (
    batch_id TEXT PRIMARY KEY REFERENCES batches(batch_id),
    window_id TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    compute_fixed INTEGER NOT NULL,
    memory_fixed INTEGER NOT NULL,
    committed_clock_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_runtime (
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_batch_id TEXT,
    PRIMARY KEY(replica_id, replica_version)
);

CREATE TABLE IF NOT EXISTS lifecycle_events (
    event_key TEXT PRIMARY KEY,
    request_seq INTEGER NOT NULL,
    from_status TEXT,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    clock_ms INTEGER NOT NULL,
    window_id TEXT,
    batch_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_requests_status_arrival
    ON requests(status, arrival_ms, seq);
CREATE INDEX IF NOT EXISTS idx_windows_status_end
    ON windows(status, end_ms, replica_id);
CREATE INDEX IF NOT EXISTS idx_batches_window
    ON batches(window_id, ordinal);
"""


def _replica_from_dict(data: dict[str, Any]) -> ReplicaSpec:
    return ReplicaSpec(
        replica_id=data["replica_id"],
        compute_units=float(data["compute_units"]),
        memory_mib=int(data["memory_mib"]),
        batch_window_ms=int(data["batch_window_ms"]),
        max_batch_size=int(data["max_batch_size"]),
        max_sequence_length=int(data["max_sequence_length"]),
        max_concurrent_batches=int(data["max_concurrent_batches"]),
    )


def _model_from_dict(data: dict[str, Any]) -> ModelSpec:
    return ModelSpec(
        model_id=data["model_id"],
        layers=tuple(
            LayerSpec(name=item["name"], parameters=int(item["parameters"]))
            for item in data["layers"]
        ),
        input_tokens=int(data["input_tokens"]),
        output_tokens=int(data["output_tokens"]),
        bytes_per_parameter=int(data["bytes_per_parameter"]),
        activation_bytes_per_token=int(data["activation_bytes_per_token"]),
        latency_ms_per_compute_unit=float(data["latency_ms_per_compute_unit"]),
    )


def _precision_from_dict(data: dict[str, Any]) -> PrecisionConfig:
    return PrecisionConfig(
        compute_scale=int(data["compute_scale"]),
        memory_scale=int(data["memory_scale"]),
        latency_scale=int(data["latency_scale"]),
        tolerance=float(data["tolerance"]),
        rounding=data.get("rounding", "half_up"),
    )


def _scheduler_from_dict(data: dict[str, Any]) -> SchedulerConfig:
    return SchedulerConfig(
        logical_time_budget_ms=int(data["logical_time_budget_ms"]),
        max_concurrent_batches=int(data["max_concurrent_batches"]),
    )


def _execution_from_dict(data: dict[str, Any]) -> ExecutionConfig:
    return ExecutionConfig(
        mode=data.get("mode", "deterministic"),
        fail_batches=tuple(data.get("fail_batches", [])),
        latency_overhead_ms=int(data.get("latency_overhead_ms", 0)),
    )


class Store:
    def __init__(self, path: str | Path, *, create: bool = False) -> None:
        self.path = Path(path)
        if not self.path.exists():
            if not create:
                raise BusinessError(
                    E_DB_NOT_INITIALIZED,
                    "database does not exist",
                    details={"path": str(self.path)},
                )
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(self.path),
            timeout=30.0,
            isolation_level=None,
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA synchronous = FULL")
        if create:
            self._initialize()
        else:
            self._assert_initialized()
        journal_mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            self.conn.execute("PRAGMA journal_mode = WAL")
            journal_mode = self.conn.execute("PRAGMA journal_mode").fetchone()[0]
        if str(journal_mode).lower() != "wal":
            raise BusinessError(
                E_DB_INTEGRITY,
                "SQLite WAL mode could not be enabled",
                details={"journal_mode": journal_mode},
            )

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def lock(self) -> FileLock:
        return FileLock(self.path.with_name(self.path.name + ".lock"))

    def _initialize(self) -> None:
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA_SQL)
        with self.transaction():
            existing = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('logical_clock_ms', '0')"
                )
                self.conn.execute(
                    "INSERT INTO meta(key, value) VALUES ('workflow_phase', 'empty')"
                )

    def _assert_initialized(self) -> None:
        try:
            row = self.conn.execute(
                "SELECT value FROM meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.DatabaseError as exc:
            raise BusinessError(
                E_DB_NOT_INITIALIZED,
                "database is not a dynabatch state file",
                details={"path": str(self.path), "error": str(exc)},
            ) from exc
        if row is None or int(row["value"]) != SCHEMA_VERSION:
            raise BusinessError(
                E_DB_NOT_INITIALIZED,
                "unsupported or missing state schema",
                details={
                    "path": str(self.path),
                    "schema_version": None if row is None else row["value"],
                },
            )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else str(row["value"])

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            """
            INSERT INTO meta(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )

    def logical_clock(self) -> int:
        value = self.get_meta("logical_clock_ms")
        return 0 if value is None else int(value)

    def advance_logical_clock(self, value: int) -> int:
        current = self.logical_clock()
        new_value = max(current, int(value))
        if new_value != current:
            self.set_meta("logical_clock_ms", str(new_value))
        return new_value

    def current_version_number(self) -> int | None:
        value = self.get_meta("current_replica_version")
        return None if value is None else int(value)

    def get_model(self) -> ModelSpec:
        value = self.get_meta("model_json")
        if value is None:
            raise BusinessError(E_DB_NOT_INITIALIZED, "model state is missing")
        return _model_from_dict(json.loads(value))

    def get_precision(self) -> PrecisionConfig:
        value = self.get_meta("precision_json")
        if value is None:
            raise BusinessError(E_DB_NOT_INITIALIZED, "precision state is missing")
        return _precision_from_dict(json.loads(value))

    def get_scheduler_config(self) -> SchedulerConfig:
        value = self.get_meta("scheduler_json")
        if value is None:
            raise BusinessError(E_DB_NOT_INITIALIZED, "scheduler state is missing")
        return _scheduler_from_dict(json.loads(value))

    def get_execution_config(self) -> ExecutionConfig:
        value = self.get_meta("execution_json")
        if value is None:
            return ExecutionConfig("deterministic", (), 0)
        return _execution_from_dict(json.loads(value))

    def get_version(self, version: int) -> ReplicaVersion:
        row = self.conn.execute(
            "SELECT * FROM replica_versions WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            raise BusinessError(
                E_VERSION_CONFLICT,
                "replica version is not present",
                details={"version": version},
            )
        replicas = tuple(_replica_from_dict(item) for item in json.loads(row["replicas_json"]))
        return ReplicaVersion(
            version=int(row["version"]),
            effective_at_ms=int(row["effective_at_ms"]),
            replicas=replicas,
            fingerprint=str(row["fingerprint"]),
        )

    def get_versions(self) -> tuple[ReplicaVersion, ...]:
        rows = self.conn.execute(
            "SELECT version FROM replica_versions ORDER BY version"
        ).fetchall()
        return tuple(self.get_version(int(row["version"])) for row in rows)

    def version_at(self, logical_ms: int) -> ReplicaVersion:
        current = self.current_version_number()
        if current is None:
            raise BusinessError(E_DB_NOT_INITIALIZED, "no replica version is installed")
        row = self.conn.execute(
            """
            SELECT version FROM replica_versions
            WHERE effective_at_ms <= ? AND version <= ?
            ORDER BY effective_at_ms DESC, version DESC
            LIMIT 1
            """,
            (int(logical_ms), current),
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT version FROM replica_versions WHERE version <= ? ORDER BY version LIMIT 1",
                (current,),
            ).fetchone()
        if row is None:
            raise BusinessError(E_DB_NOT_INITIALIZED, "no usable replica version")
        return self.get_version(int(row["version"]))

    def get_requests(self) -> tuple[RequestSpec, ...]:
        rows = self.conn.execute(
            "SELECT * FROM requests ORDER BY arrival_ms, seq"
        ).fetchall()
        return tuple(
            RequestSpec(
                seq=int(row["seq"]),
                arrival_ms=int(row["arrival_ms"]),
                priority=int(row["priority"]),
                input_tokens=int(row["input_tokens"]),
                max_output_tokens=int(row["max_output_tokens"]),
                deadline_ms=int(row["deadline_ms"]),
            )
            for row in rows
        )

    def get_request_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM requests ORDER BY seq").fetchall()

    def _assert_existing_config(self, bundle: Bundle) -> None:
        checks = [
            ("model_json", canonical_json(bundle.model.to_dict())),
            ("precision_json", canonical_json(bundle.precision.to_dict())),
            ("scheduler_json", canonical_json(bundle.scheduler.to_dict())),
            ("execution_json", canonical_json(bundle.execution.to_dict())),
        ]
        for key, new_value in checks:
            existing = self.get_meta(key)
            if existing is not None and canonical_json(json.loads(existing)) != canonical_json(
                json.loads(new_value)
            ):
                raise BusinessError(
                    E_CONFIG_CONFLICT,
                    "bundle configuration differs from the persisted configuration",
                    details={"key": key},
                )

    def _classify_transition(
        self, previous: ReplicaVersion | None, new: ReplicaVersion
    ) -> str:
        if previous is None:
            return "complete_replace"
        old_by_id = previous.by_id()
        new_by_id = new.by_id()
        if old_by_id == new_by_id:
            return "noop"
        old_ids = set(old_by_id)
        new_ids = set(new_by_id)
        if old_ids.issubset(new_ids) and all(
            old_by_id[replica_id] == new_by_id[replica_id] for replica_id in old_ids
        ):
            return "incremental_append"
        return "complete_replace"

    def ingest_bundle(self, bundle: Bundle) -> dict[str, Any]:
        with self.transaction():
            empty = self.get_meta("model_json") is None
            actions: list[dict[str, Any]] = []
            versions_added: list[int] = []
            if empty:
                self.set_meta("model_json", canonical_json(bundle.model.to_dict()))
                self.set_meta("precision_json", canonical_json(bundle.precision.to_dict()))
                self.set_meta("scheduler_json", canonical_json(bundle.scheduler.to_dict()))
                self.set_meta("execution_json", canonical_json(bundle.execution.to_dict()))
                current = 0
            else:
                self._assert_existing_config(bundle)
                current_value = self.current_version_number()
                if current_value is None:
                    raise BusinessError(E_DB_NOT_INITIALIZED, "current replica version is missing")
                current = current_value
                bundle_max = bundle.current_version
                if bundle_max < current:
                    raise BusinessError(
                        E_VERSION_ROLLBACK,
                        "replica version rollback is forbidden",
                        details={"current": current, "submitted": bundle_max},
                    )
                current_snapshot = self.get_version(current)
                last_effective = current_snapshot.effective_at_ms
                for version in sorted(
                    bundle.replica_versions, key=lambda item: item.version
                ):
                    if version.version <= current:
                        continue
                    if version.effective_at_ms < last_effective:
                        raise BusinessError(
                            E_VERSION,
                            "new replica version effective time precedes the current snapshot",
                            details={
                                "version": version.version,
                                "effective_at_ms": version.effective_at_ms,
                                "current_effective_at_ms": last_effective,
                            },
                        )
                    last_effective = version.effective_at_ms
                for version in bundle.replica_versions:
                    if version.version <= current:
                        existing = self.get_version(version.version)
                        if existing.fingerprint != version.fingerprint:
                            raise BusinessError(
                                E_VERSION_CONFLICT,
                                "existing replica version has different contents",
                                details={"version": version.version},
                            )

            existing_numbers = {
                int(row["version"])
                for row in self.conn.execute("SELECT version FROM replica_versions")
            }
            previous = (
                self.get_version(max(existing_numbers)) if existing_numbers else None
            )
            for version in sorted(bundle.replica_versions, key=lambda item: item.version):
                if version.version in existing_numbers:
                    continue
                if version.version < current:
                    raise BusinessError(
                        E_VERSION_ROLLBACK,
                        "cannot insert a replica version below the current pointer",
                        details={"version": version.version, "current": current},
                    )
                classification = self._classify_transition(previous, version)
                self.conn.execute(
                    """
                    INSERT INTO replica_versions(
                        version, effective_at_ms, replicas_json, fingerprint,
                        source_digest, created_clock_ms
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.version,
                        version.effective_at_ms,
                        canonical_json([replica.to_dict() for replica in version.replicas]),
                        version.fingerprint,
                        bundle.digest,
                        self.logical_clock(),
                    ),
                )
                versions_added.append(version.version)
                actions.append(
                    {
                        "version": version.version,
                        "classification": classification,
                        "replica_ids": [replica.replica_id for replica in version.replicas],
                    }
                )
                previous = version

            requests_added = 0
            requests_unchanged = 0
            for request in bundle.requests:
                existing = self.conn.execute(
                    "SELECT * FROM requests WHERE seq = ?", (request.seq,)
                ).fetchone()
                if existing is None:
                    self.conn.execute(
                        """
                        INSERT INTO requests(
                            seq, arrival_ms, priority, input_tokens, max_output_tokens,
                            deadline_ms, status, updated_clock_ms
                        ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
                        """,
                        (
                            request.seq,
                            request.arrival_ms,
                            request.priority,
                            request.input_tokens,
                            request.max_output_tokens,
                            request.deadline_ms,
                            self.logical_clock(),
                        ),
                    )
                    requests_added += 1
                else:
                    existing_spec = RequestSpec(
                        seq=int(existing["seq"]),
                        arrival_ms=int(existing["arrival_ms"]),
                        priority=int(existing["priority"]),
                        input_tokens=int(existing["input_tokens"]),
                        max_output_tokens=int(existing["max_output_tokens"]),
                        deadline_ms=int(existing["deadline_ms"]),
                    )
                    if existing_spec != request:
                        raise BusinessError(
                            E_REQUEST_CONFLICT,
                            "request sequence already exists with different contents",
                            details={"seq": request.seq},
                        )
                    requests_unchanged += 1

            if versions_added:
                new_current = max(versions_added + [current])
                self.set_meta("current_replica_version", str(new_current))
            elif current:
                self.set_meta("current_replica_version", str(current))
            self.set_meta("bundle_digest", bundle.digest)
            if empty:
                self.set_meta("workflow_phase", "saved")
            if versions_added or empty:
                action = "created" if empty else "updated"
            elif requests_added:
                action = "updated"
            else:
                action = "noop"
            if not actions and current:
                current_snapshot = self.get_version(current)
                bundle_snapshot = bundle.version_map().get(current)
                if bundle_snapshot is None:
                    raise BusinessError(
                        E_VERSION_CONFLICT,
                        "bundle does not contain the persisted current version",
                        details={"current": current},
                    )
                actions.append(
                    {
                        "version": current,
                        "classification": self._classify_transition(
                            current_snapshot, bundle_snapshot
                        ),
                        "replica_ids": [
                            replica.replica_id for replica in bundle_snapshot.replicas
                        ],
                    }
                )
            return {
                "action": action,
                "versions_added": versions_added,
                "requests_added": requests_added,
                "requests_unchanged": requests_unchanged,
                "transitions": actions,
                "current_replica_version": self.current_version_number(),
                "bundle_digest": bundle.digest,
            }

    def integrity_check(self) -> str:
        row = self.conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, parameters)

    def fetchall(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, parameters).fetchall()

    def fetchone(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, parameters).fetchone()
