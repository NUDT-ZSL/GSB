from __future__ import annotations

import json
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import KernelError, fail
from .fslock import FileLock, stable_json
from .planner import plan_hash
from .validation import (
    Dataset,
    ModelSpec,
    PrecisionConfig,
    ReplicaManifest,
    ReplicaSpec,
    RequestSpec,
    build_candidates,
    estimate_individual,
    parse_model,
    parse_precision,
    parse_replicas,
    parse_requests,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_snapshot (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    content_hash TEXT NOT NULL,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS precision_snapshot (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    content_hash TEXT NOT NULL,
    json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_versions (
    version INTEGER PRIMARY KEY,
    manifest_hash TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    change_kind TEXT NOT NULL,
    created_clock_ms REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_current (
    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS replica_specs (
    replica_version INTEGER NOT NULL,
    replica_id TEXT NOT NULL,
    available_compute_units REAL NOT NULL,
    memory_limit_mib INTEGER NOT NULL,
    batch_window_ms INTEGER NOT NULL,
    max_batch_size INTEGER NOT NULL,
    max_sequence_length INTEGER NOT NULL,
    PRIMARY KEY (replica_version, replica_id)
);

CREATE TABLE IF NOT EXISTS requests (
    request_seq INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL UNIQUE,
    arrival_ms INTEGER NOT NULL,
    priority INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL,
    max_output_tokens INTEGER NOT NULL,
    deadline_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(
        status IN ('pending','batched','executing','completed','expired')
    ),
    outcome TEXT,
    failure_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    assigned_version INTEGER,
    assigned_replica_id TEXT,
    window_key TEXT,
    batch_key TEXT,
    updated_clock_ms REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS request_candidates (
    request_seq INTEGER NOT NULL,
    replica_version INTEGER NOT NULL,
    replica_id TEXT NOT NULL,
    sequence_ok INTEGER NOT NULL,
    compute_ok INTEGER NOT NULL,
    memory_ok INTEGER NOT NULL,
    PRIMARY KEY (request_seq, replica_version, replica_id)
);

CREATE TABLE IF NOT EXISTS windows (
    window_key TEXT PRIMARY KEY,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    window_start_ms INTEGER NOT NULL,
    window_end_ms INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','prepared','committed')),
    plan_hash TEXT,
    plan_json TEXT,
    request_count INTEGER NOT NULL DEFAULT 0,
    batch_count INTEGER NOT NULL DEFAULT 0,
    precision_sensitive INTEGER NOT NULL DEFAULT 0,
    error_bound_max REAL NOT NULL DEFAULT 0,
    created_clock_ms REAL NOT NULL,
    committed_clock_ms REAL,
    UNIQUE (replica_id, replica_version, window_start_ms)
);

CREATE TABLE IF NOT EXISTS window_plan_requests (
    window_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL UNIQUE,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (window_key, request_seq)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_key TEXT PRIMARY KEY,
    window_key TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    request_seqs_json TEXT NOT NULL,
    request_ids_json TEXT NOT NULL,
    decision_json TEXT NOT NULL,
    estimate_json TEXT NOT NULL,
    error_spectrum_json TEXT NOT NULL,
    precision_sensitive INTEGER NOT NULL DEFAULT 0,
    first_constraint TEXT,
    compute_used REAL NOT NULL,
    memory_used REAL NOT NULL,
    estimated_completion_ms REAL NOT NULL,
    usage_released INTEGER NOT NULL DEFAULT 0,
    committed_clock_ms REAL NOT NULL,
    UNIQUE (window_key, ordinal)
);

CREATE TABLE IF NOT EXISTS batch_requests (
    batch_key TEXT NOT NULL,
    window_key TEXT NOT NULL,
    request_seq INTEGER NOT NULL UNIQUE,
    replica_version INTEGER NOT NULL,
    ordinal INTEGER NOT NULL,
    PRIMARY KEY (batch_key, request_seq),
    UNIQUE (request_seq, replica_version)
);

CREATE TABLE IF NOT EXISTS request_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_seq INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    reason TEXT NOT NULL,
    clock_ms REAL NOT NULL,
    batch_key TEXT
);

CREATE TABLE IF NOT EXISTS precision_flags (
    scope TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    difference REAL NOT NULL,
    tolerance REAL NOT NULL,
    chosen TEXT NOT NULL,
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    error_chain_json TEXT NOT NULL,
    created_clock_ms REAL NOT NULL,
    PRIMARY KEY (scope, scope_key, metric)
);

CREATE TABLE IF NOT EXISTS replica_state (
    replica_id TEXT NOT NULL,
    replica_version INTEGER NOT NULL,
    next_window_start_ms INTEGER NOT NULL DEFAULT 0,
    committed_windows INTEGER NOT NULL DEFAULT 0,
    committed_batches INTEGER NOT NULL DEFAULT 0,
    precision_sensitive_count INTEGER NOT NULL DEFAULT 0,
    error_bound_sum REAL NOT NULL DEFAULT 0,
    error_bound_max REAL NOT NULL DEFAULT 0,
    budget_exceeded_batches INTEGER NOT NULL DEFAULT 0,
    compute_in_use REAL NOT NULL DEFAULT 0,
    memory_in_use_mib REAL NOT NULL DEFAULT 0,
    last_clock_ms REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (replica_id, replica_version)
);

CREATE INDEX IF NOT EXISTS idx_windows_state
    ON windows(replica_id, replica_version, status);
CREATE INDEX IF NOT EXISTS idx_batches_active
    ON batches(replica_id, replica_version, usage_released);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
"""


def _row_to_request(row: sqlite3.Row) -> RequestSpec:
    return RequestSpec(
        request_seq=int(row["request_seq"]),
        request_id=str(row["request_id"]),
        arrival_ms=int(row["arrival_ms"]),
        priority=int(row["priority"]),
        input_tokens=int(row["input_tokens"]),
        max_output_tokens=int(row["max_output_tokens"]),
        deadline_ms=int(row["deadline_ms"]),
    )


def _row_to_replica(row: sqlite3.Row) -> ReplicaSpec:
    return ReplicaSpec(
        replica_id=str(row["replica_id"]),
        available_compute_units=float(row["available_compute_units"]),
        memory_limit_mib=int(row["memory_limit_mib"]),
        batch_window_ms=int(row["batch_window_ms"]),
        max_batch_size=int(row["max_batch_size"]),
        max_sequence_length=int(row["max_sequence_length"]),
    )


def _json(text: str) -> Any:
    return json.loads(text)


def _quantities(estimate: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        estimate["compute"],
        estimate["memory"],
        estimate["queue_ms"],
        estimate["efficiency"],
    ]


class Store:
    def __init__(self, db_path: str | Path, lock_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.lock_path = Path(lock_path)
        self._lock = FileLock(self.lock_path)

    def lock(self) -> FileLock:
        return self._lock

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=30.0,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @contextmanager
    def tx(self, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
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

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            "PRAGMA journal_mode = WAL; PRAGMA synchronous = FULL;\n" + SCHEMA
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version','1')"
        )
        conn.execute(
            "INSERT OR IGNORE INTO meta(key,value) "
            "VALUES('logical_clock_watermark_ms','0')"
        )

    def schema_exists(self) -> bool:
        if not self.db_path.exists():
            return False
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def _clock(self, conn: sqlite3.Connection) -> float:
        row = conn.execute(
            "SELECT value FROM meta WHERE key='logical_clock_watermark_ms'"
        ).fetchone()
        return float(row["value"]) if row is not None else 0.0

    def _set_clock(self, conn: sqlite3.Connection, value: float) -> None:
        previous = self._clock(conn)
        encoded = repr(max(previous, float(value)))
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('logical_clock_watermark_ms',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (encoded,),
        )

    def current_version(self, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT version FROM replica_current WHERE singleton=1"
        ).fetchone()
        if row is None:
            fail("E_NO_SNAPSHOT", "replica snapshot is not loaded")
        return int(row["version"])

    def current_replicas(self, conn: sqlite3.Connection) -> list[ReplicaSpec]:
        version = self.current_version(conn)
        rows = conn.execute(
            "SELECT * FROM replica_specs WHERE replica_version=? "
            "ORDER BY replica_id",
            (version,),
        ).fetchall()
        return [_row_to_replica(row) for row in rows]

    def _insert_version(
        self,
        conn: sqlite3.Connection,
        manifest: ReplicaManifest,
        change_kind: str,
    ) -> None:
        conn.execute(
            "INSERT INTO replica_versions"
            "(version,manifest_hash,manifest_json,change_kind,created_clock_ms) "
            "VALUES(?,?,?,?,?)",
            (
                manifest.version,
                manifest.content_hash,
                stable_json(manifest.raw),
                change_kind,
                self._clock(conn),
            ),
        )
        for replica in manifest.replicas:
            conn.execute(
                "INSERT INTO replica_specs"
                "(replica_version,replica_id,available_compute_units,"
                "memory_limit_mib,batch_window_ms,max_batch_size,"
                "max_sequence_length) VALUES(?,?,?,?,?,?,?)",
                (
                    manifest.version,
                    replica.replica_id,
                    replica.available_compute_units,
                    replica.memory_limit_mib,
                    replica.batch_window_ms,
                    replica.max_batch_size,
                    replica.max_sequence_length,
                ),
            )
            conn.execute(
                "INSERT INTO replica_state(replica_id,replica_version) "
                "VALUES(?,?) ON CONFLICT DO NOTHING",
                (replica.replica_id, manifest.version),
            )

    def _ensure_state_rows(
        self, conn: sqlite3.Connection, version: int, replicas: list[ReplicaSpec]
    ) -> None:
        for replica in replicas:
            conn.execute(
                "INSERT INTO replica_state(replica_id,replica_version) "
                "VALUES(?,?) ON CONFLICT DO NOTHING",
                (replica.replica_id, version),
            )

    def load(self, dataset: Dataset) -> dict[str, Any]:
        with self.lock():
            schema_conn = self.connect()
            try:
                self._ensure_schema(schema_conn)
            finally:
                schema_conn.close()
            with self.tx() as conn:
                model_row = conn.execute(
                    "SELECT content_hash FROM model_snapshot WHERE singleton=1"
                ).fetchone()
                if model_row is None:
                    conn.execute(
                        "INSERT INTO model_snapshot(singleton,content_hash,json) "
                        "VALUES(1,?,?)",
                        (
                            dataset.model.content_hash,
                            stable_json(dataset.model.raw),
                        ),
                    )
                elif model_row["content_hash"] != dataset.model.content_hash:
                    fail(
                        "E_STATE_CONFLICT",
                        "model description differs from the persisted snapshot",
                    )

                precision_row = conn.execute(
                    "SELECT content_hash FROM precision_snapshot WHERE singleton=1"
                ).fetchone()
                if precision_row is None:
                    conn.execute(
                        "INSERT INTO precision_snapshot"
                        "(singleton,content_hash,json) VALUES(1,?,?)",
                        (
                            dataset.precision.content_hash,
                            stable_json(dataset.precision.raw),
                        ),
                    )
                elif precision_row["content_hash"] != dataset.precision.content_hash:
                    fail(
                        "E_STATE_CONFLICT",
                        "precision configuration differs from persisted state",
                    )

                current_row = conn.execute(
                    "SELECT version FROM replica_current WHERE singleton=1"
                ).fetchone()
                change_kind = "initial"
                if current_row is None:
                    self._insert_version(conn, dataset.replicas, "initial")
                    conn.execute(
                        "INSERT INTO replica_current(singleton,version) VALUES(1,?)",
                        (dataset.replicas.version,),
                    )
                else:
                    current = int(current_row["version"])
                    requested = dataset.replicas.version
                    if requested < current:
                        fail(
                            "E_VERSION_ROLLBACK",
                            "replica version rollback is forbidden",
                            current_version=current,
                            requested_version=requested,
                        )
                    current_hash_row = conn.execute(
                        "SELECT manifest_hash FROM replica_versions WHERE version=?",
                        (current,),
                    ).fetchone()
                    current_hash = (
                        str(current_hash_row["manifest_hash"])
                        if current_hash_row is not None
                        else None
                    )
                    if current_hash == dataset.replicas.content_hash:
                        change_kind = "no_op"
                    elif requested == current:
                        fail(
                            "E_VERSION_CONFLICT",
                            "replica content changed without a new version",
                            version=current,
                        )
                    else:
                        old_rows = conn.execute(
                            "SELECT * FROM replica_specs "
                            "WHERE replica_version=? ORDER BY replica_id",
                            (current,),
                        ).fetchall()
                        old_specs = {
                            row["replica_id"]: _row_to_replica(row)
                            for row in old_rows
                        }
                        new_specs = dataset.replicas.by_id
                        if all(
                            replica_id in new_specs
                            and old_specs[replica_id] == new_specs[replica_id]
                            for replica_id in old_specs
                        ):
                            change_kind = "incremental_append"
                        else:
                            change_kind = "full_replace"
                        self._insert_version(
                            conn, dataset.replicas, change_kind
                        )
                        conn.execute(
                            "UPDATE replica_current SET version=? WHERE singleton=1",
                            (dataset.replicas.version,),
                        )

                active_version = self.current_version(conn)
                active_replicas = self.current_replicas(conn)
                inserted_requests = 0
                for request in dataset.requests.requests:
                    existing = conn.execute(
                        "SELECT * FROM requests WHERE request_seq=? "
                        "OR request_id=?",
                        (request.request_seq, request.request_id),
                    ).fetchone()
                    if existing is not None:
                        existing_spec = _row_to_request(existing)
                        if existing_spec != request:
                            fail(
                                "E_STATE_CONFLICT",
                                "request trace conflicts with persisted request",
                                request_seq=request.request_seq,
                                request_id=request.request_id,
                            )
                        continue
                    conn.execute(
                        "INSERT INTO requests"
                        "(request_seq,request_id,arrival_ms,priority,input_tokens,"
                        "max_output_tokens,deadline_ms,status,failure_count,"
                        "updated_clock_ms) "
                        "VALUES(?,?,?,?,?,?,?,'pending',0,?)",
                        (
                            request.request_seq,
                            request.request_id,
                            request.arrival_ms,
                            request.priority,
                            request.input_tokens,
                            request.max_output_tokens,
                            request.deadline_ms,
                            self._clock(conn),
                        ),
                    )
                    inserted_requests += 1

                pending_rows = conn.execute(
                    "SELECT * FROM requests WHERE status='pending' "
                    "ORDER BY arrival_ms, request_seq"
                ).fetchall()
                pending = [_row_to_request(row) for row in pending_rows]
                candidates = build_candidates(
                    dataset.model,
                    ReplicaManifest(
                        version=active_version,
                        replicas=tuple(active_replicas),
                        raw={},
                        content_hash="",
                    ),
                    pending,
                )
                for request in pending:
                    replica_by_id = {
                        item.replica_id: item for item in active_replicas
                    }
                    for replica_id in candidates[request.request_seq]:
                        replica = replica_by_id[replica_id]
                        conn.execute(
                            "INSERT INTO request_candidates"
                            "(request_seq,replica_version,replica_id,sequence_ok,"
                            "compute_ok,memory_ok) VALUES(?,?,?,1,1,1) "
                            "ON CONFLICT(request_seq,replica_version,replica_id) "
                            "DO NOTHING",
                            (
                                request.request_seq,
                                active_version,
                                replica_id,
                            ),
                        )
                self._ensure_state_rows(
                    conn, active_version, active_replicas
                )
                conn.execute(
                    "INSERT INTO meta(key,value) VALUES('active_replica_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(active_version),),
                )
                return {
                    "ok": True,
                    "action": "load",
                    "active_replica_version": active_version,
                    "change_kind": change_kind,
                    "inserted_requests": inserted_requests,
                    "total_requests": int(
                        conn.execute(
                            "SELECT COUNT(*) AS n FROM requests"
                        ).fetchone()["n"]
                    ),
                    "pending_requests": int(
                        conn.execute(
                            "SELECT COUNT(*) AS n FROM requests "
                            "WHERE status='pending'"
                        ).fetchone()["n"]
                    ),
                    "logical_clock_watermark_ms": self._clock(conn),
                }

    def load_dataset_from_db(
        self, conn: sqlite3.Connection
    ) -> tuple[ModelSpec, PrecisionConfig, ReplicaManifest, list[ReplicaSpec]]:
        model_row = conn.execute(
            "SELECT json FROM model_snapshot WHERE singleton=1"
        ).fetchone()
        precision_row = conn.execute(
            "SELECT json FROM precision_snapshot WHERE singleton=1"
        ).fetchone()
        if model_row is None or precision_row is None:
            fail("E_NO_SNAPSHOT", "dataset snapshots are not loaded")
        model = parse_model(_json(model_row["json"]))
        precision = parse_precision(_json(precision_row["json"]))
        version = self.current_version(conn)
        version_row = conn.execute(
            "SELECT manifest_json FROM replica_versions WHERE version=?",
            (version,),
        ).fetchone()
        if version_row is None:
            fail("E_NO_SNAPSHOT", "active replica version is missing")
        manifest = parse_replicas(_json(version_row["manifest_json"]))
        replicas = self.current_replicas(conn)
        return model, precision, manifest, replicas

    def pending_requests(self, conn: sqlite3.Connection) -> list[RequestSpec]:
        rows = conn.execute(
            "SELECT r.* FROM requests r "
            "WHERE r.status='pending' "
            "AND NOT EXISTS (SELECT 1 FROM window_plan_requests p "
            "                WHERE p.request_seq=r.request_seq) "
            "ORDER BY r.arrival_ms, r.request_seq"
        ).fetchall()
        return [_row_to_request(row) for row in rows]

    def candidate_map(
        self,
        conn: sqlite3.Connection,
        requests: list[RequestSpec],
        model: ModelSpec,
        replicas: list[ReplicaSpec],
        version: int,
    ) -> dict[int, tuple[str, ...]]:
        candidates = build_candidates(
            model,
            ReplicaManifest(
                version=version,
                replicas=tuple(replicas),
                raw={},
                content_hash="",
            ),
            requests,
        )
        replica_by_id = {item.replica_id: item for item in replicas}
        for request in requests:
            for replica_id in candidates[request.request_seq]:
                replica = replica_by_id[replica_id]
                compute, memory = estimate_individual(request, replica, model)
                conn.execute(
                    "INSERT INTO request_candidates"
                    "(request_seq,replica_version,replica_id,sequence_ok,"
                    "compute_ok,memory_ok) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(request_seq,replica_version,replica_id) "
                    "DO UPDATE SET sequence_ok=excluded.sequence_ok,"
                    "compute_ok=excluded.compute_ok,memory_ok=excluded.memory_ok",
                    (
                        request.request_seq,
                        version,
                        replica_id,
                        int(request.total_tokens <= replica.max_sequence_length),
                        int(compute <= replica.available_compute_units),
                        int(memory <= replica.memory_limit_mib),
                    ),
                )
        return candidates

    def next_window_starts(
        self, conn: sqlite3.Connection, version: int, replicas: list[ReplicaSpec]
    ) -> dict[str, int]:
        result = {item.replica_id: 0 for item in replicas}
        rows = conn.execute(
            "SELECT replica_id,next_window_start_ms FROM replica_state "
            "WHERE replica_version=?",
            (version,),
        ).fetchall()
        for row in rows:
            result[str(row["replica_id"])] = int(row["next_window_start_ms"])
        return result

    def _insert_flags(
        self, conn: sqlite3.Connection, plan: dict[str, Any]
    ) -> None:
        clock = float(plan["decision_clock_ms"])
        for flag in plan.get("precision_flags", []):
            conn.execute(
                "INSERT INTO precision_flags"
                "(scope,scope_key,metric,difference,tolerance,chosen,"
                "replica_id,replica_version,error_chain_json,created_clock_ms) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope,scope_key,metric) DO NOTHING",
                (
                    flag["scope"],
                    flag["scope_key"],
                    flag["metric"],
                    float(flag["difference"]),
                    float(flag["tolerance"]),
                    str(flag["chosen"]),
                    str(flag["replica_id"]),
                    int(plan["replica_version"]),
                    stable_json(flag["error_chain"]),
                    clock,
                ),
            )

    def prepare_window(self, plan: dict[str, Any]) -> str:
        digest = plan_hash(plan)
        with self.tx() as conn:
            existing = conn.execute(
                "SELECT status,plan_hash FROM windows WHERE window_key=?",
                (plan["window_key"],),
            ).fetchone()
            if existing is not None:
                if existing["plan_hash"] != digest:
                    fail(
                        "E_WINDOW_CONFLICT",
                        "window already exists with a different plan",
                        window_key=plan["window_key"],
                    )
                return str(existing["status"])
            error_bound_max = max(
                (
                    quantity["accumulated_error_bound"]
                    for batch in plan["batches"]
                    for quantity in _quantities(batch["estimate"])
                ),
                default=0.0,
            )
            conn.execute(
                "INSERT INTO windows"
                "(window_key,replica_id,replica_version,window_start_ms,"
                "window_end_ms,status,plan_hash,plan_json,request_count,"
                "batch_count,precision_sensitive,error_bound_max,"
                "created_clock_ms) "
                "VALUES(?,?,?,?,?,'prepared',?,?,?,?,?,?,?)",
                (
                    plan["window_key"],
                    plan["replica_id"],
                    plan["replica_version"],
                    plan["window_start_ms"],
                    plan["window_end_ms"],
                    digest,
                    stable_json(plan),
                    len(plan["requests"]),
                    len(plan["batches"]),
                    int(any(batch["precision_sensitive"] for batch in plan["batches"])),
                    error_bound_max,
                    float(plan["decision_clock_ms"]),
                ),
            )
            for ordinal, request in enumerate(plan["requests"]):
                try:
                    conn.execute(
                        "INSERT INTO window_plan_requests"
                        "(window_key,request_seq,ordinal) VALUES(?,?,?)",
                        (
                            plan["window_key"],
                            request["request_seq"],
                            ordinal,
                        ),
                    )
                except sqlite3.IntegrityError:
                    fail(
                        "E_REQUEST_ALREADY_PLANNED",
                        "request is already reserved by another window",
                        request_seq=request["request_seq"],
                    )
            self._insert_flags(conn, plan)
        return "prepared"

    def prepared_windows(self) -> list[dict[str, Any]]:
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT plan_json FROM windows WHERE status='prepared' "
                "ORDER BY window_start_ms, replica_id, replica_version"
            ).fetchall()
            return [_json(row["plan_json"]) for row in rows]
        finally:
            conn.close()

    def _release_batch_usage(
        self, conn: sqlite3.Connection, batch_key: str, clock: float
    ) -> bool:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_key=? AND usage_released=0",
            (batch_key,),
        ).fetchone()
        if batch is None:
            return False
        request_seqs = [int(value) for value in _json(batch["request_seqs_json"])]
        statuses = [
            conn.execute(
                "SELECT status FROM requests WHERE request_seq=?",
                (seq,),
            ).fetchone()["status"]
            for seq in request_seqs
        ]
        if any(status in ("pending", "batched", "executing") for status in statuses):
            return False
        conn.execute(
            "UPDATE batches SET usage_released=1 WHERE batch_key=?",
            (batch_key,),
        )
        conn.execute(
            "UPDATE replica_state SET "
            "compute_in_use=COALESCE(("
            "  SELECT SUM(b.compute_used) FROM batches b "
            "  WHERE b.replica_id=replica_state.replica_id "
            "  AND b.replica_version=replica_state.replica_version "
            "  AND b.usage_released=0"
            "),0.0),"
            "memory_in_use_mib=COALESCE(("
            "  SELECT SUM(b.memory_used) FROM batches b "
            "  WHERE b.replica_id=replica_state.replica_id "
            "  AND b.replica_version=replica_state.replica_version "
            "  AND b.usage_released=0"
            "),0.0),"
            "last_clock_ms=MAX(last_clock_ms,?) "
            "WHERE replica_id=? AND replica_version=?",
            (
                clock,
                batch["replica_id"],
                batch["replica_version"],
            ),
        )
        return True

    def commit_window(
        self,
        plan: dict[str, Any],
        *,
        crash_hook: Callable[[str], None] | None = None,
    ) -> str:
        digest = plan_hash(plan)
        with self.tx() as conn:
            row = conn.execute(
                "SELECT * FROM windows WHERE window_key=?",
                (plan["window_key"],),
            ).fetchone()
            if row is None:
                fail(
                    "E_WINDOW_NOT_PREPARED",
                    "window must be persisted as prepared before commit",
                    window_key=plan["window_key"],
                )
            if row["status"] == "committed":
                if row["plan_hash"] != digest:
                    fail(
                        "E_WINDOW_CONFLICT",
                        "idempotent commit received a different plan",
                        window_key=plan["window_key"],
                    )
                return "already_committed"
            if row["status"] != "prepared" or row["plan_hash"] != digest:
                fail(
                    "E_WINDOW_CONFLICT",
                    "window status or plan hash is inconsistent",
                    window_key=plan["window_key"],
                )

            clock = float(plan["decision_clock_ms"])
            total_compute = 0.0
            total_memory = 0.0
            total_error = 0.0
            error_max = 0.0
            budget_exceeded = 0
            sensitive = 0
            for batch in plan["batches"]:
                estimate = batch["estimate"]
                error_spectrum = {
                    "compute": estimate["compute"],
                    "memory": estimate["memory"],
                    "queue_ms": estimate["queue_ms"],
                    "efficiency": estimate["efficiency"],
                    "within_error_budget": estimate["within_error_budget"],
                    "aggregation_order": estimate["aggregation_order"],
                    "request_ids": estimate["request_ids"],
                }
                conn.execute(
                    "INSERT INTO batches"
                    "(batch_key,window_key,replica_id,replica_version,ordinal,"
                    "request_seqs_json,request_ids_json,decision_json,"
                    "estimate_json,error_spectrum_json,precision_sensitive,"
                    "first_constraint,compute_used,memory_used,"
                    "estimated_completion_ms,usage_released,committed_clock_ms) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?)",
                    (
                        batch["batch_key"],
                        plan["window_key"],
                        plan["replica_id"],
                        plan["replica_version"],
                        int(batch["ordinal"]),
                        stable_json(batch["request_seqs"]),
                        stable_json(batch["request_ids"]),
                        stable_json(batch["decision"]),
                        stable_json(estimate),
                        stable_json(error_spectrum),
                        int(bool(batch["precision_sensitive"])),
                        batch["decision"].get("first_constraint"),
                        float(batch["compute_used"]),
                        float(batch["memory_used"]),
                        float(batch["estimated_completion_ms"]),
                        clock,
                    ),
                )
                for ordinal, request_seq in enumerate(batch["request_seqs"]):
                    conn.execute(
                        "INSERT INTO batch_requests"
                        "(batch_key,window_key,request_seq,replica_version,"
                        "ordinal) VALUES(?,?,?,?,?)",
                        (
                            batch["batch_key"],
                            plan["window_key"],
                            request_seq,
                            plan["replica_version"],
                            ordinal,
                        ),
                    )
                    request = conn.execute(
                        "SELECT * FROM requests WHERE request_seq=?",
                        (request_seq,),
                    ).fetchone()
                    if request is None:
                        fail(
                            "E_REQUEST_MISSING",
                            "planned request does not exist",
                            request_seq=request_seq,
                        )
                    if request["status"] == "pending":
                        conn.execute(
                            "UPDATE requests SET status='batched',"
                            "assigned_version=?,assigned_replica_id=?,"
                            "window_key=?,batch_key=?,updated_clock_ms=?,"
                            "outcome=NULL WHERE request_seq=?",
                            (
                                plan["replica_version"],
                                plan["replica_id"],
                                plan["window_key"],
                                batch["batch_key"],
                                clock,
                                request_seq,
                            ),
                        )
                        conn.execute(
                            "INSERT INTO request_events"
                            "(request_seq,request_id,from_status,to_status,"
                            "reason,clock_ms,batch_key) "
                            "VALUES(?,?,'pending','batched','WINDOW_COMMITTED',?,?)",
                            (
                                request_seq,
                                request["request_id"],
                                clock,
                                batch["batch_key"],
                            ),
                        )
                    elif (
                        request["status"] != "batched"
                        or request["batch_key"] != batch["batch_key"]
                    ):
                        fail(
                            "E_REQUEST_STATE",
                            "request is not pending for batch commit",
                            request_seq=request_seq,
                            status=request["status"],
                        )

                total_compute += float(batch["compute_used"])
                total_memory += float(batch["memory_used"])
                batch_error = max(
                    quantity["accumulated_error_bound"]
                    for quantity in _quantities(estimate)
                )
                total_error += batch_error
                error_max = max(error_max, batch_error)
                if not estimate["within_error_budget"]:
                    budget_exceeded += 1
                if batch["precision_sensitive"]:
                    sensitive += 1

            if crash_hook is not None:
                crash_hook("inside_window_commit")

            flags = plan.get("precision_flags", [])
            sensitive += len(flags)
            conn.execute(
                "UPDATE replica_state SET "
                "next_window_start_ms=MAX(next_window_start_ms,?),"
                "committed_windows=committed_windows+1,"
                "committed_batches=committed_batches+?,"
                "precision_sensitive_count=precision_sensitive_count+?,"
                "error_bound_sum=error_bound_sum+?,"
                "error_bound_max=MAX(error_bound_max,?),"
                "budget_exceeded_batches=budget_exceeded_batches+?,"
                "compute_in_use=compute_in_use+?,"
                "memory_in_use_mib=memory_in_use_mib+?,"
                "last_clock_ms=MAX(last_clock_ms,?) "
                "WHERE replica_id=? AND replica_version=?",
                (
                    int(plan["window_end_ms"]),
                    len(plan["batches"]),
                    sensitive,
                    total_error,
                    error_max,
                    budget_exceeded,
                    total_compute,
                    total_memory,
                    clock,
                    plan["replica_id"],
                    plan["replica_version"],
                ),
            )
            conn.execute(
                "UPDATE windows SET status='committed',"
                "committed_clock_ms=? WHERE window_key=?",
                (clock, plan["window_key"]),
            )
            self._set_clock(conn, clock)
        return "committed"

    def active_completion_horizon(self) -> float:
        conn = self.connect()
        try:
            row = conn.execute(
                "SELECT MAX(estimated_completion_ms) AS horizon "
                "FROM batches WHERE usage_released=0"
            ).fetchone()
            return float(row["horizon"] or 0.0)
        finally:
            conn.close()

    def settle_due(self, clock: float) -> dict[str, Any]:
        transitioned = 0
        released = 0
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM batches WHERE usage_released=0 "
                "AND estimated_completion_ms<=? "
                "ORDER BY estimated_completion_ms, batch_key",
                (float(clock),),
            ).fetchall()
            for batch in rows:
                request_seqs = [
                    int(value) for value in _json(batch["request_seqs_json"])
                ]
                all_terminal = True
                for request_seq in request_seqs:
                    request = conn.execute(
                        "SELECT * FROM requests WHERE request_seq=?",
                        (request_seq,),
                    ).fetchone()
                    if request is None:
                        all_terminal = False
                        continue
                    final_status = str(request["status"])
                    if request["status"] in ("pending", "batched"):
                        conn.execute(
                            "UPDATE requests SET status='executing',"
                            "updated_clock_ms=? WHERE request_seq=?",
                            (float(clock), request_seq),
                        )
                        conn.execute(
                            "INSERT INTO request_events"
                            "(request_seq,request_id,from_status,to_status,"
                            "reason,clock_ms,batch_key) "
                            "VALUES(?,?,?,'executing','BATCH_STARTED',?,?)",
                            (
                                request_seq,
                                request["request_id"],
                                request["status"],
                                float(clock),
                                batch["batch_key"],
                            ),
                        )
                        request = conn.execute(
                            "SELECT * FROM requests WHERE request_seq=?",
                            (request_seq,),
                        ).fetchone()
                        final_status = "executing"
                    if request["status"] == "executing":
                        if float(batch["estimated_completion_ms"]) > float(
                            request["deadline_ms"]
                        ):
                            terminal = "expired"
                            outcome = "DEADLINE_MISSED"
                        else:
                            terminal = "completed"
                            outcome = "SUCCESS"
                        conn.execute(
                            "UPDATE requests SET status=?,outcome=?,"
                            "failure_count=0,last_error=NULL,"
                            "updated_clock_ms=? WHERE request_seq=?",
                            (terminal, outcome, float(clock), request_seq),
                        )
                        conn.execute(
                            "INSERT INTO request_events"
                            "(request_seq,request_id,from_status,to_status,"
                            "reason,clock_ms,batch_key) "
                            "VALUES(?,?,'executing',?,?,?,?)",
                            (
                                request_seq,
                                request["request_id"],
                                terminal,
                                outcome,
                                float(clock),
                                batch["batch_key"],
                            ),
                        )
                        final_status = terminal
                        transitioned += 1
                    if final_status not in (
                        "completed",
                        "expired",
                    ):
                        all_terminal = False
                if all_terminal and self._release_batch_usage(
                    conn, batch["batch_key"], float(clock)
                ):
                    released += 1
            self._set_clock(conn, float(clock))
        return {
            "settled_batches": len(rows),
            "request_transitions": transitioned,
            "released_batches": released,
            "clock_watermark_ms": clock,
        }

    def record_execution_failure(
        self, request_id: str, attempts: int, error: str
    ) -> dict[str, Any]:
        if attempts < 1:
            fail("E_BAD_INPUT", "attempts must be positive")
        with self.lock():
            with self.tx() as conn:
                request = conn.execute(
                    "SELECT * FROM requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if request is None:
                    fail(
                        "E_REQUEST_NOT_FOUND",
                        "request_id does not exist",
                        request_id=request_id,
                    )
                if request["status"] in ("completed", "expired"):
                    fail(
                        "E_REQUEST_STATE",
                        "terminal request cannot fail again",
                        request_id=request_id,
                        status=request["status"],
                    )
                if request["status"] in ("pending",) and request["batch_key"] is None:
                    fail(
                        "E_REQUEST_STATE",
                        "request is not assigned to a committed batch",
                        request_id=request_id,
                    )
                clock = self._clock(conn)
                from_status = request["status"]
                if from_status == "batched":
                    conn.execute(
                        "UPDATE requests SET status='executing' "
                        "WHERE request_seq=?",
                        (request["request_seq"],),
                    )
                    conn.execute(
                        "INSERT INTO request_events"
                        "(request_seq,request_id,from_status,to_status,reason,"
                        "clock_ms,batch_key) "
                        "VALUES(?,?,'batched','executing','FAILURE_OBSERVED',?,?)",
                        (
                            request["request_seq"],
                            request_id,
                            clock,
                            request["batch_key"],
                        ),
                    )
                    from_status = "executing"
                if from_status != "executing":
                    fail(
                        "E_REQUEST_STATE",
                        "request must be batched or executing to record failure",
                        request_id=request_id,
                        status=from_status,
                    )
                conn.execute(
                    "UPDATE requests SET status='expired',"
                    "outcome='EXECUTION_FAILED',failure_count=failure_count+?,"
                    "last_error=?,updated_clock_ms=? WHERE request_seq=?",
                    (
                        attempts,
                        error,
                        clock,
                        request["request_seq"],
                    ),
                )
                conn.execute(
                    "INSERT INTO request_events"
                    "(request_seq,request_id,from_status,to_status,reason,"
                    "clock_ms,batch_key) "
                    "VALUES(?,?,'executing','expired','EXECUTION_FAILED',?,?)",
                    (
                        request["request_seq"],
                        request_id,
                        clock,
                        request["batch_key"],
                    ),
                )
                released = 0
                if request["batch_key"] is not None:
                    if self._release_batch_usage(
                        conn, str(request["batch_key"]), clock
                    ):
                        released = 1
                return {
                    "ok": True,
                    "action": "fail",
                    "request_id": request_id,
                    "outcome": "EXECUTION_FAILED",
                    "failure_count": int(request["failure_count"]) + attempts,
                    "released_batch": bool(released),
                }

    def _request_counts(
        self, conn: sqlite3.Connection
    ) -> dict[str, int]:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM requests GROUP BY status "
            "ORDER BY status"
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def status(self, details: bool = False) -> dict[str, Any]:
        if not self.schema_exists():
            fail("E_NO_STATE", "scheduler database does not exist")
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            version = self.current_version(conn)
            replicas = self.current_replicas(conn)
            model_row = conn.execute(
                "SELECT content_hash FROM model_snapshot WHERE singleton=1"
            ).fetchone()
            precision_row = conn.execute(
                "SELECT content_hash,json FROM precision_snapshot WHERE singleton=1"
            ).fetchone()
            precision = (
                parse_precision(_json(precision_row["json"]))
                if precision_row is not None
                else None
            )
            flag_counts = {
                (row["replica_id"], int(row["replica_version"])): int(row["n"])
                for row in conn.execute(
                    "SELECT replica_id,replica_version,COUNT(*) AS n "
                    "FROM precision_flags GROUP BY replica_id,replica_version"
                ).fetchall()
            }
            replica_reports: list[dict[str, Any]] = []
            for replica in replicas:
                state = conn.execute(
                    "SELECT * FROM replica_state "
                    "WHERE replica_id=? AND replica_version=?",
                    (replica.replica_id, version),
                ).fetchone()
                window_row = conn.execute(
                    "SELECT COUNT(*) AS n FROM windows "
                    "WHERE replica_id=? AND replica_version=? "
                    "AND status='prepared'",
                    (replica.replica_id, version),
                ).fetchone()
                replica_reports.append(
                    {
                        "replica_id": replica.replica_id,
                        "replica_version": version,
                        "window_watermark_ms": (
                            int(state["next_window_start_ms"])
                            if state is not None
                            else 0
                        ),
                        "committed_windows": (
                            int(state["committed_windows"])
                            if state is not None
                            else 0
                        ),
                        "prepared_windows": int(window_row["n"]),
                        "committed_batches": (
                            int(state["committed_batches"])
                            if state is not None
                            else 0
                        ),
                        "precision_sensitive_count": flag_counts.get(
                            (replica.replica_id, version), 0
                        ),
                        "error_bound_sum": (
                            float(state["error_bound_sum"])
                            if state is not None
                            else 0.0
                        ),
                        "error_bound_max": (
                            float(state["error_bound_max"])
                            if state is not None
                            else 0.0
                        ),
                        "budget_exceeded_batches": (
                            int(state["budget_exceeded_batches"])
                            if state is not None
                            else 0
                        ),
                        "compute_in_use": (
                            float(state["compute_in_use"])
                            if state is not None
                            else 0.0
                        ),
                        "memory_in_use_mib": (
                            float(state["memory_in_use_mib"])
                            if state is not None
                            else 0.0
                        ),
                    }
                )

            total_flags = int(
                conn.execute("SELECT COUNT(*) AS n FROM precision_flags").fetchone()[
                    "n"
                ]
            )
            version_states = []
            for state in conn.execute(
                "SELECT * FROM replica_state "
                "ORDER BY replica_version, replica_id"
            ).fetchall():
                version_states.append(
                    {
                        "replica_id": state["replica_id"],
                        "replica_version": int(state["replica_version"]),
                        "window_watermark_ms": int(
                            state["next_window_start_ms"]
                        ),
                        "committed_windows": int(
                            state["committed_windows"]
                        ),
                        "committed_batches": int(
                            state["committed_batches"]
                        ),
                        "precision_sensitive_count": flag_counts.get(
                            (
                                state["replica_id"],
                                int(state["replica_version"]),
                            ),
                            0,
                        ),
                        "error_bound_sum": float(state["error_bound_sum"]),
                        "error_bound_max": float(state["error_bound_max"]),
                        "budget_exceeded_batches": int(
                            state["budget_exceeded_batches"]
                        ),
                        "compute_in_use": float(state["compute_in_use"]),
                        "memory_in_use_mib": float(
                            state["memory_in_use_mib"]
                        ),
                    }
                )
            outcome_counts = {
                str(row["outcome"]): int(row["n"])
                for row in conn.execute(
                    "SELECT outcome,COUNT(*) AS n FROM requests "
                    "WHERE outcome IS NOT NULL GROUP BY outcome "
                    "ORDER BY outcome"
                ).fetchall()
            }
            result: dict[str, Any] = {
                "ok": True,
                "action": "status",
                "active_replica_version": version,
                "logical_clock_watermark_ms": self._clock(conn),
                "model_hash": (
                    str(model_row["content_hash"])
                    if model_row is not None
                    else None
                ),
                "precision_config_hash": (
                    str(precision_row["content_hash"])
                    if precision_row is not None
                    else None
                ),
                "error_budget": (
                    float(precision.error_budget)
                    if precision is not None
                    else None
                ),
                "float_tolerance": (
                    float(precision.float_tolerance)
                    if precision is not None
                    else None
                ),
                "replicas": replica_reports,
                "version_states": version_states,
                "request_counts": self._request_counts(conn),
                "outcome_counts": outcome_counts,
                "precision_sensitive_total": total_flags,
                "prepared_windows_total": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM windows WHERE status='prepared'"
                    ).fetchone()["n"]
                ),
                "committed_windows_total": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM windows WHERE status='committed'"
                    ).fetchone()["n"]
                ),
                "committed_batches_total": int(
                    conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
                ),
            }
            if details:
                flags = conn.execute(
                    "SELECT * FROM precision_flags "
                    "ORDER BY replica_version, replica_id, scope, scope_key, metric"
                ).fetchall()
                result["precision_flags"] = [
                    {
                        "scope": row["scope"],
                        "scope_key": row["scope_key"],
                        "metric": row["metric"],
                        "difference": float(row["difference"]),
                        "tolerance": float(row["tolerance"]),
                        "chosen": row["chosen"],
                        "replica_id": row["replica_id"],
                        "replica_version": int(row["replica_version"]),
                        "created_clock_ms": float(row["created_clock_ms"]),
                        "error_chain": _json(row["error_chain_json"]),
                    }
                    for row in flags
                ]
                batch_rows = conn.execute(
                    "SELECT * FROM batches "
                    "ORDER BY replica_version, replica_id, window_key, ordinal"
                ).fetchall()
                result["batches"] = [
                    {
                        "batch_key": row["batch_key"],
                        "window_key": row["window_key"],
                        "replica_id": row["replica_id"],
                        "replica_version": int(row["replica_version"]),
                        "ordinal": int(row["ordinal"]),
                        "request_seqs": _json(row["request_seqs_json"]),
                        "request_ids": _json(row["request_ids_json"]),
                        "precision_sensitive": bool(row["precision_sensitive"]),
                        "first_constraint": row["first_constraint"],
                        "compute_used": float(row["compute_used"]),
                        "memory_used": float(row["memory_used"]),
                        "estimated_completion_ms": float(
                            row["estimated_completion_ms"]
                        ),
                        "usage_released": bool(row["usage_released"]),
                        "error_spectrum": _json(row["error_spectrum_json"]),
                        "decision": _json(row["decision_json"]),
                    }
                    for row in batch_rows
                ]
                window_rows = conn.execute(
                    "SELECT * FROM windows "
                    "ORDER BY replica_version, replica_id, window_start_ms"
                ).fetchall()
                result["windows"] = [
                    {
                        "window_key": row["window_key"],
                        "replica_id": row["replica_id"],
                        "replica_version": int(row["replica_version"]),
                        "window_start_ms": int(row["window_start_ms"]),
                        "window_end_ms": int(row["window_end_ms"]),
                        "status": row["status"],
                        "request_count": int(row["request_count"]),
                        "batch_count": int(row["batch_count"]),
                        "plan_hash": row["plan_hash"],
                        "precision_sensitive": bool(
                            row["precision_sensitive"]
                        ),
                        "error_bound_max": float(row["error_bound_max"]),
                    }
                    for row in window_rows
                ]
            return result
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            conn.close()

    def verify(self) -> dict[str, Any]:
        errors: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        stats: dict[str, Any] = {}
        if not self.schema_exists():
            return {
                "ok": False,
                "errors": [{"code": "E_NO_STATE", "message": "database missing"}],
                "warnings": [],
                "stats": {},
            }
        conn = self.connect()
        try:
            conn.execute("BEGIN")
            try:
                version = self.current_version(conn)
            except KernelError as exc:
                return {
                    "ok": False,
                    "errors": [exc.to_dict()],
                    "warnings": [],
                    "stats": {},
                }
            if (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM model_snapshot WHERE singleton=1"
                ).fetchone()["n"]
                != 1
            ):
                errors.append(
                    {"code": "E_MODEL_MISSING", "message": "model snapshot missing"}
                )
            if (
                conn.execute(
                    "SELECT COUNT(*) AS n FROM precision_snapshot WHERE singleton=1"
                ).fetchone()["n"]
                != 1
            ):
                errors.append(
                    {
                        "code": "E_PRECISION_MISSING",
                        "message": "precision snapshot missing",
                    }
                )
            precision_row = conn.execute(
                "SELECT json FROM precision_snapshot WHERE singleton=1"
            ).fetchone()
            precision_budget = (
                parse_precision(_json(precision_row["json"])).error_budget
                if precision_row is not None
                else None
            )

            windows = conn.execute(
                "SELECT * FROM windows ORDER BY window_start_ms, replica_id, "
                "replica_version"
            ).fetchall()
            for row in windows:
                try:
                    plan = _json(row["plan_json"])
                except (TypeError, json.JSONDecodeError):
                    errors.append(
                        {
                            "code": "E_WINDOW_PLAN",
                            "message": "window plan JSON is invalid",
                            "window_key": row["window_key"],
                        }
                    )
                    continue
                if plan_hash(plan) != row["plan_hash"]:
                    errors.append(
                        {
                            "code": "E_PLAN_HASH",
                            "message": "window plan hash mismatch",
                            "window_key": row["window_key"],
                        }
                    )
                if plan["window_key"] != row["window_key"]:
                    errors.append(
                        {
                            "code": "E_PLAN_KEY",
                            "message": "plan/window key mismatch",
                            "window_key": row["window_key"],
                        }
                    )
                if int(plan["replica_version"]) != int(row["replica_version"]):
                    errors.append(
                        {
                            "code": "E_VERSION_MIX",
                            "message": "window mixes replica versions",
                            "window_key": row["window_key"],
                        }
                    )
                reserved = {
                    int(item["request_seq"]) for item in plan["requests"]
                }
                row_reserved = {
                    int(item["request_seq"])
                    for item in conn.execute(
                        "SELECT request_seq FROM window_plan_requests "
                        "WHERE window_key=?",
                        (row["window_key"],),
                    ).fetchall()
                }
                if reserved != row_reserved:
                    errors.append(
                        {
                            "code": "E_PLAN_REQUESTS",
                            "message": "window reservation set does not match plan",
                            "window_key": row["window_key"],
                        }
                    )
                if row["status"] == "committed":
                    batches = conn.execute(
                        "SELECT * FROM batches WHERE window_key=? "
                        "ORDER BY ordinal",
                        (row["window_key"],),
                    ).fetchall()
                    if len(batches) != int(row["batch_count"]):
                        errors.append(
                            {
                                "code": "E_BATCH_COUNT",
                                "message": "window batch count mismatch",
                                "window_key": row["window_key"],
                            }
                        )
                    batch_seqs: list[int] = []
                    for batch in batches:
                        if (
                            batch["replica_id"] != row["replica_id"]
                            or int(batch["replica_version"])
                            != int(row["replica_version"])
                        ):
                            errors.append(
                                {
                                    "code": "E_BATCH_VERSION",
                                    "message": "batch replica/version mismatch",
                                    "batch_key": batch["batch_key"],
                                }
                            )
                        seqs = [
                            int(item)
                            for item in _json(batch["request_seqs_json"])
                        ]
                        batch_seqs.extend(seqs)
                        members = {
                            int(item["request_seq"])
                            for item in conn.execute(
                                "SELECT request_seq FROM batch_requests "
                                "WHERE batch_key=?",
                                (batch["batch_key"],),
                            ).fetchall()
                        }
                        if members != set(seqs):
                            errors.append(
                                {
                                    "code": "E_BATCH_MEMBERS",
                                    "message": "batch membership mismatch",
                                    "batch_key": batch["batch_key"],
                                }
                            )
                        estimate = _json(batch["estimate_json"])
                        spectrum = _json(batch["error_spectrum_json"])
                        for quantity in _quantities(estimate):
                            bound = float(
                                quantity["accumulated_error_bound"]
                            )
                            expected_within = (
                                bound <= precision_budget + 1e-15
                                if precision_budget is not None
                                else bool(quantity["within_error_budget"])
                            )
                            if bool(quantity["within_error_budget"]) != expected_within:
                                errors.append(
                                    {
                                        "code": "E_ERROR_BUDGET_FLAG",
                                        "message": (
                                            "error-budget flag is inconsistent "
                                            "with the accumulated bound"
                                        ),
                                        "batch_key": batch["batch_key"],
                                    }
                                )
                            if (
                                precision_budget is not None
                                and bound > precision_budget + 1e-15
                            ):
                                warnings.append(
                                    {
                                        "code": "W_ERROR_BUDGET",
                                        "message": "batch estimate exceeds configured budget",
                                        "batch_key": batch["batch_key"],
                                        "quantity": quantity["raw"],
                                        "error_bound": bound,
                                        "budget": precision_budget,
                                    }
                                )
                        for quantity_name in (
                            "compute",
                            "memory",
                            "queue_ms",
                            "efficiency",
                        ):
                            if (
                                spectrum[quantity_name]["fixed"]
                                != estimate[quantity_name]["fixed"]
                                or spectrum[quantity_name]["scale"]
                                != estimate[quantity_name]["scale"]
                            ):
                                errors.append(
                                    {
                                        "code": "E_SPECTRUM_MISMATCH",
                                        "message": (
                                            "persisted spectrum differs from "
                                            "the batch estimate"
                                        ),
                                        "batch_key": batch["batch_key"],
                                    }
                                )
                        if batch["precision_sensitive"]:
                            flag_count = conn.execute(
                                "SELECT COUNT(*) AS n FROM precision_flags "
                                "WHERE scope='batch_composition' "
                                "AND scope_key=?",
                                (batch["batch_key"],),
                            ).fetchone()["n"]
                            if int(flag_count) == 0:
                                errors.append(
                                    {
                                        "code": "E_SENSITIVE_FLAG_MISSING",
                                        "message": (
                                            "sensitive batch has no error-chain flag"
                                        ),
                                        "batch_key": batch["batch_key"],
                                    }
                                )
                        released_statuses = {
                            str(item["status"])
                            for item in conn.execute(
                                "SELECT status FROM requests "
                                "WHERE request_seq IN "
                                f"({','.join('?' for _ in seqs)})",
                                tuple(seqs),
                            ).fetchall()
                        } if seqs else set()
                        if batch["usage_released"] and released_statuses & {
                            "pending",
                            "batched",
                            "executing",
                        }:
                            errors.append(
                                {
                                    "code": "E_USAGE_RELEASE",
                                    "message": "usage released while request remains active",
                                    "batch_key": batch["batch_key"],
                                }
                            )
                    if sorted(batch_seqs) != sorted(reserved):
                        errors.append(
                            {
                                "code": "E_WINDOW_BATCH_REQUESTS",
                                "message": "batch request union differs from plan",
                                "window_key": row["window_key"],
                            }
                        )
                    if len(batch_seqs) != len(set(batch_seqs)):
                        errors.append(
                            {
                                "code": "E_DUP_BATCH_REQUEST",
                                "message": "request appears in multiple batches",
                                "window_key": row["window_key"],
                            }
                        )
                    for seq in reserved:
                        request = conn.execute(
                            "SELECT * FROM requests WHERE request_seq=?",
                            (seq,),
                        ).fetchone()
                        if request is None:
                            errors.append(
                                {
                                    "code": "E_REQUEST_MISSING",
                                    "message": "planned request missing",
                                    "request_seq": seq,
                                }
                            )
                        elif request["window_key"] != row["window_key"]:
                            errors.append(
                                {
                                    "code": "E_REQUEST_WINDOW",
                                    "message": "request window binding mismatch",
                                    "request_seq": seq,
                                }
                            )
                elif row["status"] == "prepared":
                    if (
                        int(
                            conn.execute(
                                "SELECT COUNT(*) AS n FROM batches "
                                "WHERE window_key=?",
                                (row["window_key"],),
                            ).fetchone()["n"]
                        )
                        != 0
                    ):
                        errors.append(
                            {
                                "code": "E_PREPARED_BATCH",
                                "message": "prepared window already has batches",
                                "window_key": row["window_key"],
                            }
                        )
                    for seq in reserved:
                        request = conn.execute(
                            "SELECT status,batch_key FROM requests "
                            "WHERE request_seq=?",
                            (seq,),
                        ).fetchone()
                        if request is None or request["status"] != "pending":
                            errors.append(
                                {
                                    "code": "E_PREPARED_REQUEST",
                                    "message": "prepared plan request is not pending",
                                    "request_seq": seq,
                                }
                            )

            state_rows = conn.execute(
                "SELECT * FROM replica_state ORDER BY replica_version, replica_id"
            ).fetchall()
            for state in state_rows:
                active = conn.execute(
                    "SELECT COUNT(*) AS n,"
                    "COALESCE(SUM(compute_used),0.0) AS compute,"
                    "COALESCE(SUM(memory_used),0.0) AS memory "
                    "FROM batches WHERE replica_id=? AND replica_version=? "
                    "AND usage_released=0",
                    (state["replica_id"], state["replica_version"]),
                ).fetchone()
                if not math.isclose(
                    float(active["compute"]),
                    float(state["compute_in_use"]),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                ):
                    errors.append(
                        {
                            "code": "E_COMPUTE_LEDGER",
                            "message": "compute usage ledger mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                if not math.isclose(
                    float(active["memory"]),
                    float(state["memory_in_use_mib"]),
                    rel_tol=0.0,
                    abs_tol=1e-9,
                ):
                    errors.append(
                        {
                            "code": "E_MEMORY_LEDGER",
                            "message": "memory usage ledger mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                committed_counts = conn.execute(
                    "SELECT COUNT(*) AS windows,"
                    "COALESCE(SUM(batch_count),0) AS batches,"
                    "COALESCE(MAX(window_end_ms),0) AS watermark "
                    "FROM windows WHERE replica_id=? AND replica_version=? "
                    "AND status='committed'",
                    (state["replica_id"], state["replica_version"]),
                ).fetchone()
                if int(committed_counts["windows"]) != int(
                    state["committed_windows"]
                ):
                    errors.append(
                        {
                            "code": "E_WINDOW_LEDGER",
                            "message": "committed window count mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                if int(committed_counts["batches"]) != int(
                    state["committed_batches"]
                ):
                    errors.append(
                        {
                            "code": "E_BATCH_LEDGER",
                            "message": "committed batch count mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                if int(state["next_window_start_ms"]) < int(
                    committed_counts["watermark"]
                ):
                    errors.append(
                        {
                            "code": "E_WATERMARK",
                            "message": "window watermark is behind committed windows",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                batch_rows = conn.execute(
                    "SELECT error_spectrum_json,precision_sensitive FROM batches "
                    "WHERE replica_id=? AND replica_version=?",
                    (state["replica_id"], state["replica_version"]),
                ).fetchall()
                expected_error_sum = 0.0
                expected_error_max = 0.0
                expected_batch_sensitive = 0
                for batch_row in batch_rows:
                    batch_spectrum = _json(batch_row["error_spectrum_json"])
                    batch_error = max(
                        float(batch_spectrum[name]["accumulated_error_bound"])
                        for name in (
                            "compute",
                            "memory",
                            "queue_ms",
                            "efficiency",
                        )
                    )
                    expected_error_sum += batch_error
                    expected_error_max = max(
                        expected_error_max, batch_error
                    )
                    if batch_row["precision_sensitive"]:
                        expected_batch_sensitive += 1
                flag_count = 0
                for flag_row in conn.execute(
                    "SELECT scope,scope_key FROM precision_flags "
                    "WHERE replica_id=? AND replica_version=?",
                    (state["replica_id"], state["replica_version"]),
                ).fetchall():
                    if flag_row["scope"] == "batch_composition":
                        committed_batch = conn.execute(
                            "SELECT 1 FROM batches WHERE batch_key=?",
                            (flag_row["scope_key"],),
                        ).fetchone()
                        if committed_batch is not None:
                            flag_count += 1
                    elif flag_row["scope"] == "replica_choice":
                        try:
                            request_seq = int(
                                str(flag_row["scope_key"]).rsplit(":", 1)[1]
                            )
                        except (ValueError, IndexError):
                            continue
                        committed_assignment = conn.execute(
                            "SELECT 1 FROM requests r "
                            "JOIN windows w ON w.window_key=r.window_key "
                            "WHERE r.request_seq=? "
                            "AND r.assigned_version=? "
                            "AND w.status='committed'",
                            (request_seq, state["replica_version"]),
                        ).fetchone()
                        if committed_assignment is not None:
                            flag_count += 1
                    else:
                        flag_count += 1
                expected_sensitive = (
                    expected_batch_sensitive + flag_count
                )
                if not math.isclose(
                    expected_error_sum,
                    float(state["error_bound_sum"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    errors.append(
                        {
                            "code": "E_ERROR_BOUND_SUM",
                            "message": "error-bound sum ledger mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                if not math.isclose(
                    expected_error_max,
                    float(state["error_bound_max"]),
                    rel_tol=0.0,
                    abs_tol=1e-12,
                ):
                    errors.append(
                        {
                            "code": "E_ERROR_BOUND_MAX",
                            "message": "error-bound maximum ledger mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )
                if expected_sensitive != int(
                    state["precision_sensitive_count"]
                ):
                    errors.append(
                        {
                            "code": "E_SENSITIVE_LEDGER",
                            "message": "precision-sensitive count mismatch",
                            "replica_id": state["replica_id"],
                            "replica_version": int(state["replica_version"]),
                        }
                    )

            for request in conn.execute(
                "SELECT * FROM requests ORDER BY request_seq"
            ).fetchall():
                if request["status"] in ("completed", "expired"):
                    if request["batch_key"] is None:
                        errors.append(
                            {
                                "code": "E_REQUEST_TERMINAL",
                                "message": "terminal request has no batch",
                                "request_seq": int(request["request_seq"]),
                            }
                        )
                    if request["status"] == "expired" and request[
                        "outcome"
                    ] not in ("DEADLINE_MISSED", "EXECUTION_FAILED"):
                        errors.append(
                            {
                                "code": "E_EXPIRY_OUTCOME",
                                "message": "expired request outcome is ambiguous",
                                "request_seq": int(request["request_seq"]),
                            }
                        )
                if request["status"] == "pending" and request["batch_key"]:
                    errors.append(
                        {
                            "code": "E_PENDING_ASSIGNED",
                            "message": "pending request has a batch assignment",
                            "request_seq": int(request["request_seq"]),
                        }
                    )
                if int(request["failure_count"]) < 0:
                    errors.append(
                        {
                            "code": "E_FAILURE_COUNT",
                            "message": "negative failure count",
                            "request_seq": int(request["request_seq"]),
                        }
                    )

            for flag in conn.execute(
                "SELECT * FROM precision_flags ORDER BY scope,scope_key,metric"
            ).fetchall():
                if float(flag["difference"]) >= float(flag["tolerance"]):
                    errors.append(
                        {
                            "code": "E_PRECISION_FLAG",
                            "message": "precision flag difference is not below tolerance",
                            "scope_key": flag["scope_key"],
                        }
                    )
                try:
                    chain = _json(flag["error_chain_json"])
                except json.JSONDecodeError:
                    chain = None
                if not chain:
                    errors.append(
                        {
                            "code": "E_ERROR_CHAIN",
                            "message": "precision flag has no error chain",
                            "scope_key": flag["scope_key"],
                        }
                    )

            stats = {
                "active_replica_version": version,
                "windows": int(
                    conn.execute("SELECT COUNT(*) AS n FROM windows").fetchone()["n"]
                ),
                "prepared_windows": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM windows WHERE status='prepared'"
                    ).fetchone()["n"]
                ),
                "committed_windows": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM windows WHERE status='committed'"
                    ).fetchone()["n"]
                ),
                "batches": int(
                    conn.execute("SELECT COUNT(*) AS n FROM batches").fetchone()["n"]
                ),
                "request_assignments": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM batch_requests"
                    ).fetchone()["n"]
                ),
                "requests": int(
                    conn.execute("SELECT COUNT(*) AS n FROM requests").fetchone()["n"]
                ),
                "precision_flags": int(
                    conn.execute(
                        "SELECT COUNT(*) AS n FROM precision_flags"
                    ).fetchone()["n"]
                ),
                "logical_clock_watermark_ms": self._clock(conn),
                "request_counts": self._request_counts(conn),
            }
            return {
                "ok": not errors,
                "action": "verify",
                "errors": errors,
                "warnings": warnings,
                "stats": stats,
            }
        finally:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error:
                pass
            conn.close()

