from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .errors import EngineError, fail
from .locking import FileLock
from .model import ExecutionConfig, Plan, Relation
from .ops import (
    attach_watermark,
    relation_from_wire,
    relation_to_wire,
    run_node_chain,
)
from .reference import _concat_outputs
from .util import (
    canonical_json,
    sha256_bytes,
    sha256_file,
    sha256_json,
    write_bytes_atomic,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    plan_digest TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    data_digest TEXT NOT NULL,
    original_plan_json TEXT NOT NULL,
    rewritten_plan_json TEXT NOT NULL,
    config_json TEXT NOT NULL,
    rewrite_report_json TEXT NOT NULL,
    status TEXT NOT NULL,
    shard_count INTEGER NOT NULL,
    next_shard INTEGER NOT NULL DEFAULT 0,
    logical_clock_ms REAL NOT NULL DEFAULT 0,
    result_json TEXT,
    final_digest TEXT,
    error_spectrum_json TEXT,
    error_code TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shards (
    run_id TEXT NOT NULL,
    shard_id INTEGER NOT NULL,
    row_start INTEGER NOT NULL,
    row_end INTEGER NOT NULL,
    batch_ids_json TEXT NOT NULL,
    vector_width INTEGER NOT NULL,
    memory_mib REAL NOT NULL,
    logical_start_ms REAL,
    logical_end_ms REAL,
    status TEXT NOT NULL DEFAULT 'PENDING',
    reason TEXT,
    payload_path TEXT,
    digest TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    error_json TEXT,
    PRIMARY KEY (run_id, shard_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS node_states (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    watermark INTEGER NOT NULL,
    status TEXT NOT NULL,
    state_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS commits (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    shard_id INTEGER NOT NULL,
    state_version INTEGER NOT NULL,
    result_digest TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id, shard_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS node_outputs (
    run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    shard_id INTEGER NOT NULL,
    is_final INTEGER NOT NULL DEFAULT 0,
    output_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, node_id, shard_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS leases (
    lease_token TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    shard_id INTEGER NOT NULL,
    size_mib REAL NOT NULL,
    owner_pid INTEGER NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS errors (
    error_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    shard_id INTEGER,
    node_id TEXT,
    code TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_shards_status ON shards(run_id, status, shard_id);
CREATE INDEX IF NOT EXISTS idx_outputs_node ON node_outputs(run_id, node_id, shard_id);
"""


TERMINAL_SHARD_STATUSES = {"COMMITTED", "FAILED", "ABANDONED", "NOT_STARTED"}
COMMITTABLE_RUN_STATUSES = {"PENDING", "RUNNING", "INTERRUPTED", "RESUMING"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@dataclass
class ShardSpec:
    shard_id: int
    row_start: int
    row_end: int
    memory_mib: float
    batch_ids: list[int]
    logical_cost_ms: float

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.shard_id,
            self.row_start,
            self.row_end,
            canonical_json(self.batch_ids),
            0,
            self.memory_mib,
            self.logical_cost_ms,
        )


class EngineStore:
    def __init__(self, db_path: str | Path, work_dir: str | Path) -> None:
        self.db_path = Path(db_path)
        self.work_dir = Path(work_dir)
        self.payload_dir = self.work_dir / "shards"
        self.lock_dir = self.work_dir / "locks"
        self.payload_dir.mkdir(parents=True, exist_ok=True)
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    @classmethod
    def initialize(cls, db_path: str | Path, work_dir: str | Path) -> "EngineStore":
        store = cls(db_path, work_dir)
        store.db_path.parent.mkdir(parents=True, exist_ok=True)
        init_lock = FileLock(store.db_path.parent / f".{store.db_path.name}.init.lock")
        with init_lock:
            connection = sqlite3.connect(
                store.db_path,
                timeout=30.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("PRAGMA busy_timeout=30000")
                has_schema = connection.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='runs'
                    """
                ).fetchone()
                if has_schema is None:
                    connection.executescript(SCHEMA)
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO meta(key, value)
                        VALUES('schema_version', '1')
                        """
                    )
            finally:
                connection.close()
        return store

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _lock(self, run_id: str) -> FileLock:
        return FileLock(self.lock_dir / f"{run_id}.lock")

    def ensure_run(
        self,
        *,
        run_id: str,
        plan_digest: str,
        config_digest: str,
        data_digest: str,
        original_plan: dict[str, Any],
        rewritten_plan: dict[str, Any],
        config: ExecutionConfig,
        rewrite_report: dict[str, Any],
        shards: list[ShardSpec],
    ) -> dict[str, Any]:
        now = utc_now()
        with self._lock(run_id):
            with self._transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if existing is not None:
                    for field, expected in (
                        ("plan_digest", plan_digest),
                        ("config_digest", config_digest),
                        ("data_digest", data_digest),
                    ):
                        if existing[field] != expected:
                            fail(
                                "E_RUN_CONFLICT",
                                f"run {run_id} already exists with a different {field}",
                                details={"existing": existing[field], "incoming": expected},
                            )
                    return dict(existing)
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, plan_digest, config_digest, data_digest,
                        original_plan_json, rewritten_plan_json, config_json,
                        rewrite_report_json, status, shard_count, next_shard,
                        logical_clock_ms, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, 0, 0, ?, ?)
                    """,
                    (
                        run_id,
                        plan_digest,
                        config_digest,
                        data_digest,
                        canonical_json(original_plan),
                        canonical_json(rewritten_plan),
                        canonical_json(config.raw),
                        canonical_json(rewrite_report),
                        len(shards),
                        now,
                        now,
                    ),
                )
                for shard in shards:
                    connection.execute(
                        """
                        INSERT INTO shards(
                            run_id, shard_id, row_start, row_end, batch_ids_json,
                            vector_width, memory_mib, logical_end_ms, status
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
                        """,
                        (
                            run_id,
                            shard.shard_id,
                            shard.row_start,
                            shard.row_end,
                            canonical_json(shard.batch_ids),
                            config.vector_width,
                            shard.memory_mib,
                            shard.logical_cost_ms,
                        ),
                    )
                return dict(
                    connection.execute(
                        "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                    ).fetchone()
                )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["rewrite_report"] = json.loads(result["rewrite_report_json"])
            result["config"] = json.loads(result["config_json"])
            return result
        finally:
            connection.close()

    def list_shards(self, run_id: str) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT * FROM shards WHERE run_id = ? ORDER BY shard_id", (run_id,)
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def shard_counts(self, run_id: str) -> dict[str, int]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM shards WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall()
            return {row["status"]: int(row["count"]) for row in rows}
        finally:
            connection.close()

    def reset_stale_running(self, run_id: str) -> int:
        now = time.time()
        changed = 0
        with self._lock(run_id):
            with self._transaction() as connection:
                leases = connection.execute(
                    "SELECT * FROM leases WHERE run_id = ?", (run_id,)
                ).fetchall()
                active_shards: set[int] = set()
                for lease in leases:
                    if lease["expires_at"] > now and _pid_alive(int(lease["owner_pid"])):
                        active_shards.add(int(lease["shard_id"]))
                    else:
                        connection.execute(
                            "DELETE FROM leases WHERE lease_token = ?",
                            (lease["lease_token"],),
                        )
                running = connection.execute(
                    "SELECT shard_id FROM shards WHERE run_id = ? AND status = 'RUNNING'",
                    (run_id,),
                ).fetchall()
                for row in running:
                    shard_id = int(row["shard_id"])
                    if shard_id in active_shards:
                        continue
                    connection.execute(
                        """
                        UPDATE shards SET status='PENDING',
                            reason='RECOVERED_STALE_RUNNING'
                        WHERE run_id=? AND shard_id=? AND status='RUNNING'
                        """,
                        (run_id, shard_id),
                    )
                    changed += 1
                if changed:
                    connection.execute(
                        "UPDATE runs SET status='RESUMING', updated_at=? WHERE run_id=?",
                        (utc_now(), run_id),
                    )
        return changed

    def try_claim(
        self,
        *,
        run_id: str,
        shard_id: int,
        logical_start_ms: float,
        lease_seconds: float = 120.0,
    ) -> tuple[str | None, str | None]:
        now = time.time()
        with self._lock(run_id):
            with self._transaction() as connection:
                run = connection.execute(
                    "SELECT * FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    fail("E_STATE", f"run {run_id} does not exist")
                if run["status"] in {
                    "FAILED_PARTIAL",
                    "ABANDONED_PARTIAL",
                    "NOT_STARTED_PARTIAL",
                }:
                    return None, f"RUN_{run['status']}"
                shard = connection.execute(
                    "SELECT * FROM shards WHERE run_id = ? AND shard_id = ?",
                    (run_id, shard_id),
                ).fetchone()
                if shard is None:
                    fail("E_STATE", f"shard {shard_id} does not exist")
                if shard["status"] != "PENDING":
                    return None, f"STATUS_{shard['status']}"
                config = json.loads(run["config_json"])
                global_budget = float(
                    config.get(
                        "global_memory_budget_mib",
                        float(config["scan_memory_budget_mib"])
                        * int(config["max_parallel_tasks"]),
                    )
                )
                max_tasks = int(config["max_parallel_tasks"])
                connection.execute("DELETE FROM leases WHERE expires_at <= ?", (now,))
                leases = connection.execute(
                    "SELECT size_mib FROM leases WHERE run_id = ?", (run_id,)
                ).fetchall()
                if len(leases) >= max_tasks:
                    return None, "PARALLEL_TASK_CAPACITY"
                used = sum(float(row["size_mib"]) for row in leases)
                if used + float(shard["memory_mib"]) > global_budget + 1e-12:
                    return None, "GLOBAL_MEMORY_BUDGET"
                token = uuid.uuid4().hex
                connection.execute(
                    """
                    INSERT INTO leases(
                        lease_token, run_id, shard_id, size_mib, owner_pid,
                        acquired_at, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        token,
                        run_id,
                        shard_id,
                        float(shard["memory_mib"]),
                        os.getpid(),
                        now,
                        now + lease_seconds,
                    ),
                )
                connection.execute(
                    """
                    UPDATE shards SET status='RUNNING', attempt=attempt+1,
                        logical_start_ms=?, reason=NULL, started_at=?
                    WHERE run_id=? AND shard_id=? AND status='PENDING'
                    """,
                    (logical_start_ms, utc_now(), run_id, shard_id),
                )
                connection.execute(
                    "UPDATE runs SET status='RUNNING', updated_at=? WHERE run_id=?",
                    (utc_now(), run_id),
                )
                return token, None

    def release_lease(self, token: str) -> None:
        connection = self._connect()
        try:
            connection.execute("DELETE FROM leases WHERE lease_token = ?", (token,))
        finally:
            connection.close()

    def record_result(
        self,
        *,
        run_id: str,
        shard_id: int,
        lease_token: str | None,
        payload: dict[str, Any],
        logical_end_ms: float,
    ) -> str:
        payload_bytes = canonical_json(payload).encode("utf-8")
        digest = sha256_bytes(payload_bytes)
        relative = Path(run_id) / f"shard_{shard_id:08d}.json"
        path = self.payload_dir / relative
        write_bytes_atomic(path, payload_bytes)
        write_bytes_atomic(
            path.with_name(path.name + ".sha256"),
            (digest + "\n").encode("ascii"),
        )
        with self._lock(run_id):
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM shards WHERE run_id=? AND shard_id=?",
                    (run_id, shard_id),
                ).fetchone()
                if row is None:
                    fail("E_STATE", f"shard {shard_id} does not exist")
                if row["status"] == "RESULT_READY":
                    if row["digest"] != digest:
                        fail(
                            "E_DIGEST_MISMATCH",
                            "duplicate shard result has a different digest",
                            shard_id=shard_id,
                        )
                    if lease_token:
                        connection.execute(
                            "DELETE FROM leases WHERE lease_token=?", (lease_token,)
                        )
                    return digest
                if row["status"] != "RUNNING":
                    fail(
                        "E_STATE",
                        f"cannot record result for shard in status {row['status']}",
                        shard_id=shard_id,
                    )
                connection.execute(
                    """
                    UPDATE shards SET status='RESULT_READY', payload_path=?,
                        digest=?, logical_end_ms=?, reason=NULL, finished_at=?
                    WHERE run_id=? AND shard_id=?
                    """,
                    (
                        str(path),
                        digest,
                        logical_end_ms,
                        utc_now(),
                        run_id,
                        shard_id,
                    ),
                )
                if lease_token:
                    connection.execute(
                        "DELETE FROM leases WHERE lease_token=?", (lease_token,)
                    )
        return digest

    def record_failure(
        self,
        *,
        run_id: str,
        shard_id: int,
        lease_token: str | None,
        error: EngineError,
    ) -> None:
        with self._lock(run_id):
            with self._transaction() as connection:
                connection.execute(
                    """
                    UPDATE shards SET status='FAILED', reason=?, error_json=?,
                        finished_at=?
                    WHERE run_id=? AND shard_id=?
                    """,
                    (
                        error.code,
                        canonical_json(error.to_dict()),
                        utc_now(),
                        run_id,
                        shard_id,
                    ),
                )
                if lease_token:
                    connection.execute(
                        "DELETE FROM leases WHERE lease_token=?", (lease_token,)
                    )
                self._insert_error(connection, run_id, shard_id, error)
                connection.execute(
                    """
                    UPDATE runs SET status='FAILED_PARTIAL', error_code=?,
                        error_message=?, updated_at=?
                    WHERE run_id=?
                    """,
                    (error.code, error.message, utc_now(), run_id),
                )

    def record_abandoned(
        self, *, run_id: str, shard_id: int, reason: str, logical_start_ms: float
    ) -> None:
        with self._lock(run_id):
            with self._transaction() as connection:
                connection.execute(
                    """
                    UPDATE shards SET status='ABANDONED', reason=?,
                        logical_start_ms=?, finished_at=?
                    WHERE run_id=? AND shard_id=? AND status='PENDING'
                    """,
                    (reason, logical_start_ms, utc_now(), run_id, shard_id),
                )

    def record_not_started(
        self, *, run_id: str, shard_id: int, reason: str
    ) -> None:
        with self._lock(run_id):
            with self._transaction() as connection:
                connection.execute(
                    """
                    UPDATE shards SET status='NOT_STARTED', reason=?, finished_at=?
                    WHERE run_id=? AND shard_id=? AND status='PENDING'
                    """,
                    (reason, utc_now(), run_id, shard_id),
                )

    def mark_run_status(self, run_id: str, status: str, reason: str | None = None) -> None:
        with self._lock(run_id):
            with self._transaction() as connection:
                connection.execute(
                    """
                    UPDATE runs SET status=?, error_message=COALESCE(?, error_message),
                        updated_at=? WHERE run_id=?
                    """,
                    (status, reason, utc_now(), run_id),
                )

    def set_logical_clock(self, run_id: str, logical_clock_ms: float) -> float:
        with self._lock(run_id):
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT logical_clock_ms FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    fail("E_STATE", f"run {run_id} does not exist")
                current = float(row["logical_clock_ms"])
                if logical_clock_ms < current:
                    fail(
                        "E_CLOCK_REGRESSION",
                        "logical clock moved backwards",
                        details={"persisted": current, "requested": logical_clock_ms},
                    )
                connection.execute(
                    "UPDATE runs SET logical_clock_ms=?, updated_at=? WHERE run_id=?",
                    (logical_clock_ms, utc_now(), run_id),
                )
                return current

    def advance_logical_clock(self, run_id: str, proposed_ms: float) -> float:
        with self._lock(run_id):
            with self._transaction() as connection:
                row = connection.execute(
                    "SELECT logical_clock_ms FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    fail("E_STATE", f"run {run_id} does not exist")
                current = float(row["logical_clock_ms"])
                new_value = max(current, float(proposed_ms))
                if new_value > current:
                    connection.execute(
                        "UPDATE runs SET logical_clock_ms=?, updated_at=? WHERE run_id=?",
                        (new_value, utc_now(), run_id),
                    )
                return new_value

    def _load_states(
        self, connection: sqlite3.Connection, run_id: str
    ) -> tuple[dict[str, dict[str, Any]], dict[str, tuple[int, int]]]:
        rows = connection.execute(
            "SELECT * FROM node_states WHERE run_id=? ORDER BY node_id", (run_id,)
        ).fetchall()
        states: dict[str, dict[str, Any]] = {}
        versions: dict[str, tuple[int, int]] = {}
        for row in rows:
            state = json.loads(row["state_json"])
            if sha256_json(state) != row["digest"]:
                fail(
                    "E_DIGEST_MISMATCH",
                    "persisted operator state digest does not match content",
                    node_id=row["node_id"],
                )
            states[row["node_id"]] = state
            versions[row["node_id"]] = (int(row["state_version"]), int(row["watermark"]))
        return states, versions

    def _insert_error(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        shard_id: int | None,
        error: EngineError,
    ) -> None:
        connection.execute(
            """
            INSERT INTO errors(
                run_id, shard_id, node_id, code, message, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                shard_id if shard_id is not None else error.shard_id,
                error.node_id,
                error.code,
                error.message,
                canonical_json(error.details),
                utc_now(),
            ),
        )

    def commit_ready(
        self,
        *,
        run_id: str,
        plan: Plan,
        config: ExecutionConfig,
        shard_count: int,
        diagnostics_builder: Any | None = None,
    ) -> dict[str, Any]:
        committed: list[int] = []
        final_result: dict[str, Any] | None = None
        with self._lock(run_id):
            while True:
                with self._transaction() as connection:
                    run = connection.execute(
                        "SELECT * FROM runs WHERE run_id=?", (run_id,)
                    ).fetchone()
                    if run is None:
                        fail("E_STATE", f"run {run_id} does not exist")
                    if run["status"] == "COMPLETE":
                        return {
                            "committed": committed,
                            "result": json.loads(run["result_json"])
                            if run["result_json"]
                            else None,
                        }
                    next_shard = int(run["next_shard"])
                    if next_shard >= shard_count:
                        return {
                            "committed": committed,
                            "result": json.loads(run["result_json"])
                            if run["result_json"]
                            else None,
                        }
                    shard = connection.execute(
                        "SELECT * FROM shards WHERE run_id=? AND shard_id=?",
                        (run_id, next_shard),
                    ).fetchone()
                    if shard is None or shard["status"] != "RESULT_READY":
                        return {"committed": committed, "result": final_result}
                    path = Path(shard["payload_path"])
                    digest_path = path.with_name(path.name + ".sha256")
                    digest_file_matches = (
                        digest_path.exists()
                        and digest_path.read_text(encoding="ascii").strip()
                        == shard["digest"]
                    )
                    if (
                        not path.exists()
                        or sha256_file(path) != shard["digest"]
                        or not digest_file_matches
                    ):
                        error = EngineError(
                            "E_DIGEST_MISMATCH",
                            "shard payload digest does not match persisted digest",
                            shard_id=next_shard,
                            details={"path": str(path)},
                        )
                        connection.rollback()
                        with self._transaction() as failure_connection:
                            self._record_commit_failure_locked(
                                failure_connection, run_id, next_shard, error
                            )
                        raise error
                    try:
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        boundary = relation_from_wire(payload)
                        states, versions = self._load_states(connection, run_id)
                        first_stateful = next(
                            (
                                node_id
                                for node_id in plan.chain[1:]
                                if plan.nodes[node_id]["type"] in {"aggregate", "window"}
                            ),
                            None,
                        )
                        finalize = next_shard == shard_count - 1
                        node_outputs: dict[str, Relation] = {}
                        if first_stateful is None:
                            relation = boundary
                        else:
                            states, relation, node_outputs = run_node_chain(
                                plan,
                                boundary,
                                first_stateful,
                                states,
                                config,
                                shard_id=next_shard,
                                finalize=finalize,
                            )
                        now = utc_now()
                        for node_id, state in states.items():
                            state = attach_watermark(state, next_shard)
                            prior_version, prior_watermark = versions.get(node_id, (0, -1))
                            if prior_watermark >= next_shard:
                                fail(
                                    "E_STATE",
                                    "operator state watermark is not behind the shard being committed",
                                    node_id=node_id,
                                    shard_id=next_shard,
                                )
                            state_json = canonical_json(state)
                            digest = sha256_bytes(state_json.encode("utf-8"))
                            connection.execute(
                                """
                                INSERT INTO node_states(
                                    run_id, node_id, state_version, watermark, status,
                                    state_json, digest, updated_at
                                ) VALUES (?, ?, ?, ?, 'COMMITTED', ?, ?, ?)
                                ON CONFLICT(run_id, node_id) DO UPDATE SET
                                    state_version=excluded.state_version,
                                    watermark=excluded.watermark,
                                    status=excluded.status,
                                    state_json=excluded.state_json,
                                    digest=excluded.digest,
                                    updated_at=excluded.updated_at
                                """,
                                (
                                    run_id,
                                    node_id,
                                    prior_version + 1,
                                    next_shard,
                                    state_json,
                                    digest,
                                    now,
                                ),
                            )
                            existing_commit = connection.execute(
                                """
                                SELECT result_digest FROM commits
                                WHERE run_id=? AND node_id=? AND shard_id=?
                                """,
                                (run_id, node_id, next_shard),
                            ).fetchone()
                            if existing_commit is not None:
                                if existing_commit["result_digest"] != digest:
                                    fail(
                                        "E_COMMIT_CONFLICT",
                                        "same shard/node state version was committed with different content",
                                        node_id=node_id,
                                        shard_id=next_shard,
                                    )
                            else:
                                connection.execute(
                                    """
                                    INSERT INTO commits(
                                        run_id, node_id, shard_id, state_version,
                                        result_digest, committed_at
                                    ) VALUES (?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        run_id,
                                        node_id,
                                        next_shard,
                                        prior_version + 1,
                                        digest,
                                        now,
                                    ),
                                )
                        if relation is not None:
                            node_outputs.setdefault(plan.root, relation)
                        has_blocking_aggregate = any(
                            plan.nodes[node_id]["type"] == "aggregate"
                            for node_id in plan.chain
                        )
                        for node_id, output in node_outputs.items():
                            node_type = plan.nodes[node_id]["type"]
                            if node_type not in {"aggregate", "window"} and node_id != plan.root:
                                continue
                            target_shard = (
                                -1
                                if (
                                    node_id == plan.root
                                    and finalize
                                    and has_blocking_aggregate
                                )
                                else next_shard
                            )
                            wire = relation_to_wire(output)
                            output_json = canonical_json(wire)
                            output_digest = sha256_bytes(output_json.encode("utf-8"))
                            existing_output = connection.execute(
                                """
                                SELECT digest FROM node_outputs
                                WHERE run_id=? AND node_id=? AND shard_id=?
                                """,
                                (run_id, node_id, target_shard),
                            ).fetchone()
                            if existing_output is not None:
                                if existing_output["digest"] != output_digest:
                                    fail(
                                        "E_COMMIT_CONFLICT",
                                        "node output for this shard was already committed differently",
                                        node_id=node_id,
                                        shard_id=next_shard,
                                    )
                            else:
                                connection.execute(
                                    """
                                    INSERT INTO node_outputs(
                                        run_id, node_id, shard_id, is_final,
                                        output_json, digest, committed_at
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                                    """,
                                    (
                                        run_id,
                                        node_id,
                                        target_shard,
                                        int(target_shard == -1),
                                        output_json,
                                        output_digest,
                                        now,
                                    ),
                                )
                        connection.execute(
                            """
                            UPDATE shards SET status='COMMITTED', reason=NULL,
                                logical_end_ms=?, finished_at=?
                            WHERE run_id=? AND shard_id=?
                            """,
                            (
                                float(shard["logical_end_ms"] or 0.0),
                                now,
                                run_id,
                                next_shard,
                            ),
                        )
                        connection.execute(
                            """
                            UPDATE runs SET next_shard=?,
                                status=CASE
                                    WHEN status IN (
                                        'FAILED_PARTIAL', 'ABANDONED_PARTIAL',
                                        'NOT_STARTED_PARTIAL'
                                    ) THEN status
                                    ELSE 'RUNNING'
                                END,
                                updated_at=? WHERE run_id=?
                            """,
                            (next_shard + 1, now, run_id),
                        )
                        committed.append(next_shard)
                        if finalize:
                            result_relation = self._final_relation(
                                connection, run_id, plan
                            )
                            result_wire = relation_to_wire(result_relation)
                            result_json = canonical_json(result_wire)
                            final_digest = sha256_bytes(result_json.encode("utf-8"))
                            spectrum = (
                                diagnostics_builder(
                                    run_id=run_id,
                                    plan=plan,
                                    config=config,
                                    result=result_relation,
                                    connection=connection,
                                )
                                if diagnostics_builder
                                else {}
                            )
                            connection.execute(
                                """
                                UPDATE runs SET status='COMPLETE', result_json=?,
                                    final_digest=?, error_spectrum_json=?,
                                    error_code=NULL, error_message=NULL,
                                    updated_at=? WHERE run_id=?
                                """,
                                (
                                    result_json,
                                    final_digest,
                                    canonical_json(spectrum),
                                    utc_now(),
                                    run_id,
                                ),
                            )
                            final_result = {
                                "result": result_wire,
                                "final_digest": final_digest,
                                "error_spectrum": spectrum,
                            }
                    except EngineError as error:
                        connection.rollback()
                        with self._transaction() as failure_connection:
                            self._record_commit_failure_locked(
                                failure_connection, run_id, next_shard, error
                            )
                        raise
                if final_result is not None:
                    return {
                        "committed": committed,
                        "result": final_result["result"],
                        "final_digest": final_result["final_digest"],
                        "error_spectrum": final_result["error_spectrum"],
                    }

    def _record_commit_failure_locked(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        shard_id: int,
        error: EngineError,
    ) -> None:
        connection.execute(
            """
            UPDATE shards SET status='FAILED', reason=?, error_json=?,
                finished_at=? WHERE run_id=? AND shard_id=?
            """,
            (
                error.code,
                canonical_json(error.to_dict()),
                utc_now(),
                run_id,
                shard_id,
            ),
        )
        self._insert_error(connection, run_id, shard_id, error)
        connection.execute(
            """
            UPDATE runs SET status='FAILED_PARTIAL', error_code=?,
                error_message=?, updated_at=? WHERE run_id=?
            """,
            (error.code, error.message, utc_now(), run_id),
        )

    def _final_relation(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        plan: Plan,
    ) -> Relation:
        run = connection.execute(
            "SELECT * FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
        final_row = connection.execute(
            """
            SELECT output_json FROM node_outputs
            WHERE run_id=? AND node_id=? AND is_final=1
            ORDER BY shard_id DESC LIMIT 1
            """,
            (run_id, plan.root),
        ).fetchone()
        if final_row is not None:
            return relation_from_wire(json.loads(final_row["output_json"]))
        rows = connection.execute(
            """
            SELECT output_json FROM node_outputs
            WHERE run_id=? AND node_id=? AND shard_id>=0
            ORDER BY shard_id
            """,
            (run_id, plan.root),
        ).fetchall()
        if not rows:
            fail(
                "E_STATE",
                "run reached final shard without any root output",
                node_id=plan.root,
            )
        parts = [relation_from_wire(json.loads(row["output_json"])) for row in rows]
        return _concat_outputs(parts)

    def read_snapshot(self, run_id: str) -> dict[str, Any]:
        connection = self._connect()
        try:
            connection.execute("BEGIN")
            run = connection.execute(
                "SELECT * FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if run is None:
                fail("E_STATE", f"run {run_id} does not exist")
            shards = connection.execute(
                "SELECT * FROM shards WHERE run_id=? ORDER BY shard_id", (run_id,)
            ).fetchall()
            states = connection.execute(
                "SELECT * FROM node_states WHERE run_id=? ORDER BY node_id", (run_id,)
            ).fetchall()
            errors = connection.execute(
                "SELECT * FROM errors WHERE run_id=? ORDER BY error_id", (run_id,)
            ).fetchall()
            outputs = connection.execute(
                """
                SELECT node_id, shard_id, is_final, digest FROM node_outputs
                WHERE run_id=? ORDER BY node_id, shard_id
                """,
                (run_id,),
            ).fetchall()
            result = dict(run)
            if result.get("result_json"):
                result["result"] = json.loads(result["result_json"])
            if result.get("error_spectrum_json"):
                result["error_spectrum"] = json.loads(result["error_spectrum_json"])
            snapshot = {
                "run": result,
                "shards": [dict(row) for row in shards],
                "node_states": [dict(row) for row in states],
                "node_outputs": [dict(row) for row in outputs],
                "errors": [dict(row) for row in errors],
            }
            connection.commit()
            return snapshot
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def iter_outputs(
        self, run_id: str, node_id: str | None = None
    ) -> list[dict[str, Any]]:
        connection = self._connect()
        try:
            if node_id is None:
                rows = connection.execute(
                    "SELECT * FROM node_outputs WHERE run_id=? ORDER BY node_id, shard_id",
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM node_outputs
                    WHERE run_id=? AND node_id=? ORDER BY shard_id
                    """,
                    (run_id, node_id),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

